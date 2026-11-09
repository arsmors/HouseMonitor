"""Local dashboard API and process entry point."""

import asyncio
import base64
import hmac
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from backend.service import DEFAULT_INTERVAL, Monitor
from backend.sources import RECENT_LOCATION_IDS, PublicHTTP, SSAdapter, USER_AGENT
from backend.store import Store
from backend.telegram import run_bot

ROOT = Path(__file__).resolve().parent.parent


@web.middleware
async def security(request: web.Request, handler):
    """Protect local access, optional remote basic authentication, and mutation origins."""
    password = request.app["web_password"]
    hostname = request.host.split(":", 1)[0].lower()
    if not password and (request.remote not in {"127.0.0.1", "::1", None} or hostname not in {"localhost", "127.0.0.1", "[::1]"}):
        return web.json_response({"error": "Remote access requires WEB_PASSWORD"}, status=403)
    local_healthcheck = request.path == "/api/health" and request.remote in {"127.0.0.1", "::1"}
    if password and not local_healthcheck:
        expected = "Basic " + base64.b64encode(("owner:" + password).encode()).decode()
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="House Monitor"'}, text="Owner authentication required")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin")
        if request.headers.get("X-House-Monitor") != "1" or origin and urlsplit(origin).netloc != request.host:
            return web.json_response({"error": "Cross-origin request rejected"}, status=403)
    try:
        response = await handler(request)
    except (ValueError, json.JSONDecodeError) as error:
        response = web.json_response({"error": str(error)}, status=400)
    except web.HTTPException as error:
        response = error
    response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    else:
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


async def state(request: web.Request):
    """Serve current backend data, never illustrative listings or secrets."""
    monitor = request.app["monitor"]
    searches = monitor.store.searches()
    listings = [listing for listing in monitor.store.listings(10000) if set(RECENT_LOCATION_IDS).intersection(listing["location_ids"])]
    return web.json_response({"searches": searches, "listings": listings, "locations": list(monitor.store.locations().values()), "status": monitor.status()})


async def save_search(request: web.Request):
    """Create or edit one search without modifying any other profile."""
    data = await request.json()
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    identifier = int(request.match_info["id"]) if "id" in request.match_info else None
    monitor = request.app["monitor"]
    search = monitor.store.save_search(data, identifier)
    monitor.trigger()
    return web.json_response(search, status=200 if identifier else 201)


async def delete_search(request: web.Request):
    """Delete the requested search while retaining its historical records."""
    request.app["monitor"].store.delete_search(int(request.match_info["id"]))
    return web.json_response({"ok": True})


async def locations(request: web.Request):
    """Expand a validated node from the public SS.lv category hierarchy."""
    records = await request.app["monitor"].discover(request.query.get("path", ""))
    return web.json_response(records)


async def scan(request: web.Request):
    """Request a rate-limited manual scan without overlapping scheduled work."""
    if request.app["monitor"].scheduled:
        return web.json_response({"error": "Checks run hourly in GitHub Actions. Use Run workflow there for an immediate check."}, status=409)
    started = request.app["monitor"].trigger()
    return web.json_response({"started": started}, status=202 if started else 409)


async def settings(request: web.Request):
    """Persist notification preferences, never accept Telegram secrets over the UI."""
    data = await request.json()
    if not isinstance(data, dict) or not isinstance(data.get("notifications_enabled"), bool):
        raise ValueError("notifications_enabled must be a boolean")
    request.app["monitor"].store.set_setting("notifications_enabled", data["notifications_enabled"])
    return web.json_response({"ok": True})


async def health(request: web.Request):
    """Report process liveness separately from each external source's health."""
    return web.json_response({"ok": True, "scanning": request.app["monitor"].scanning})


async def index(request: web.Request):
    """Serve the built dashboard without exposing workspace configuration files."""
    file = ROOT / "dist" / "index.html"
    if not file.exists():
        return web.Response(text="Build the dashboard first: npm run build", status=503)
    return web.FileResponse(file)


async def lifecycle(app: web.Application):
    """Own the database, monitor and optional bot for the complete process lifetime."""
    store = app["monitor"].store
    monitor = app["monitor"]
    bot_task = None
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    if token and owner and (not owner.isdigit() or int(owner) <= 0):
        store.close()
        raise ValueError("TELEGRAM_OWNER_ID must be your positive private user ID")
    await monitor.start()
    if token and owner:
        bot_task = asyncio.create_task(run_bot(monitor, token, int(owner)), name="telegram")
    else:
        monitor.bot_error = "Set TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID in .env to enable Telegram"
    try:
        yield
    finally:
        if bot_task:
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        await monitor.close()
        store.close()

async def scheduled_lifecycle(app: web.Application):
    """Open only on-demand discovery resources; never start collectors in Vercel."""
    monitor = app["monitor"]
    monitor.session = aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=15),
    )
    monitor.http = PublicHTTP(monitor.session)
    monitor.ss = SSAdapter(monitor.http, monitor.store.locations())
    try:
        yield
    finally:
        await monitor.close()
        monitor.store.close()


def create_app(store: Store | None = None, start_tasks: bool = True) -> web.Application:
    """Build the web service with injectable persistence for integration tests."""
    if start_tasks:
        load_dotenv(ROOT / ".env")
    scheduled = os.getenv("MONITOR_MODE", "continuous") == "scheduled"
    if os.getenv("MONITOR_MODE", "continuous") not in {"continuous", "scheduled"}:
        raise ValueError("MONITOR_MODE must be continuous or scheduled")
    if scheduled and not os.getenv("WEB_PASSWORD"):
        raise ValueError("WEB_PASSWORD is required for the scheduled dashboard")
    app = web.Application(middlewares=[security], client_max_size=32 * 1024)
    app["web_password"] = os.getenv("WEB_PASSWORD", "")
    app["monitor"] = Monitor(store or Store.from_environment(str(ROOT / "data" / "monitor.db"), recover=not scheduled, require_remote=scheduled),
                             int(os.getenv("POLL_INTERVAL", str(DEFAULT_INTERVAL))), os.getenv("CITY24_ENABLED", "true").lower() == "true",
                             os.getenv("LATIO_PERMISSION_CONFIRMED", "false").lower() == "true", os.getenv("RENTINRIGA_ENABLED", "true").lower() == "true")
    app["monitor"].scheduled = scheduled
    app.add_routes([web.get("/api/state", state), web.post("/api/searches", save_search), web.put("/api/searches/{id:\\d+}", save_search),
                    web.delete("/api/searches/{id:\\d+}", delete_search), web.get("/api/locations", locations), web.post("/api/scan", scan),
                    web.put("/api/settings", settings), web.get("/api/health", health), web.get("/", index)])
    if (ROOT / "dist" / "assets").exists():
        app.router.add_static("/assets/", ROOT / "dist" / "assets")
    if start_tasks:
        app.cleanup_ctx.append(scheduled_lifecycle if scheduled else lifecycle)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(ROOT / ".env")
    host = os.getenv("HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv("WEB_PASSWORD"):
        raise SystemExit("Set WEB_PASSWORD before binding the dashboard to a non-loopback interface")
    web.run_app(create_app(), host=host, port=int(os.getenv("PORT", "8787")), access_log=None)