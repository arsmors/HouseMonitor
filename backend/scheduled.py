"""One bounded collection run for an hourly GitHub Actions workflow."""

import asyncio
import logging
import os
import time
import uuid

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from backend.city24_mcp_import import refresh_mcp
from backend.service import DEFAULT_INTERVAL, Monitor
from backend.sources import now
from backend.store import Store

LOGGER = logging.getLogger(__name__)
RUN_TIMEOUT = 1200


async def poll_commands(monitor: Monitor):
    """Process a bounded batch of owner-only commands without long polling."""
    store = monitor.store
    owner = monitor.owner_id
    if store.setting("telegram_owner_id") != owner:
        store.set_setting("telegram_ready", False)
        store.set_setting("telegram_owner_id", owner)
    updates = await monitor.bot.get_updates(offset=store.setting("telegram_offset", 0), timeout=0, limit=100)
    for update in updates:
        message = update.message
        if (message and message.from_user and message.from_user.id == owner
                and message.chat.type == "private" and message.chat.id == owner):
            command = (message.text or "").split(maxsplit=1)
            command = command[0].split("@", 1)[0] if command else ""
            if command == "/start":
                store.set_setting("telegram_ready", True)
                text = "House Monitor connected. Alerts are delivered during hourly checks. Manage searches and notifications in the web dashboard."
            elif command == "/status":
                text = "Scheduled hourly checks via GitHub Actions.\nLast completed check: " + str(store.setting("last_scan", "Not yet"))
            else:
                text = "Scheduled mode supports /start and /status. Commands are processed at the next hourly run. Use the web dashboard to manage searches and notifications."
            await monitor.bot.send_message(owner, text)
        store.set_setting("telegram_offset", update.update_id + 1)


async def collect(monitor: Monitor):
    """Collect all sources, enrich photos, and deliver within a hard job deadline."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    owner = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    if bool(token) != bool(owner) or owner and (not owner.isdigit() or int(owner) <= 0):
        raise ValueError("Set both TELEGRAM_BOT_TOKEN and a positive TELEGRAM_OWNER_ID, or leave both empty")
    bot = Bot(token) if token else None
    telegram_error = None
    try:
        if bot:
            monitor.owner_id = int(owner)
            try:
                identity = await bot.get_me()
                monitor.bot = bot
                monitor.bot_username = identity.username
                await asyncio.wait_for(poll_commands(monitor), timeout=60)
            except (TelegramAPIError, asyncio.TimeoutError) as error:
                telegram_error = "Scheduled Telegram connection failed: " + type(error).__name__
                monitor.bot = None
                LOGGER.error("%s", telegram_error)
        monitor.store.set_setting("scheduled_telegram", {
            "configured": bool(bot), "ready": bool(monitor.bot and monitor.store.setting("telegram_ready")),
            "username": monitor.bot_username,
            "error": telegram_error or (None if bot else "Set Telegram credentials in GitHub Actions secrets to enable alerts"),
        })
        await asyncio.wait_for(monitor.cycle(), timeout=990)
        if os.getenv("CITY24_MCP_ENABLED", "false").lower() == "true":
            try:
                result = await asyncio.wait_for(refresh_mcp(monitor.store), timeout=240)
                LOGGER.info(
                    "City24 MCP: %s listings checked, %s discovered, %s notifications queued",
                    result["total"], result["discovered"], result["queued"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                previous = monitor.store.status()["sources"].get("City24.lv", {})
                reason = str(error)[:250] if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__ + ": City24 MCP request failed"
                state = {**previous, "status": "error", "error": reason}
                monitor.store.source_state("City24.lv", state)
                monitor.store.event("City24.lv: " + reason)
                LOGGER.error("City24 MCP collection failed: %s", reason)
        try:
            await asyncio.wait_for(monitor.enrich_images(), timeout=90)
        except asyncio.TimeoutError:
            monitor.store.event("Scheduled photo enrichment reached its time budget; remaining photos wait until the next run")
            LOGGER.warning("Scheduled photo enrichment reached its time budget")
        if monitor.bot and monitor.store.setting("telegram_ready"):
            await asyncio.wait_for(monitor.deliver(), timeout=60)
        if telegram_error:
            raise RuntimeError(telegram_error)
        failed = [
            f"{source}: {state.get('error') or 'unknown source error'}"
            for source, state in monitor.store.status()["sources"].items()
            if state.get("status") == "error"
        ]
        if failed:
            raise RuntimeError("Source checks failed: " + "; ".join(failed))
    finally:
        monitor.bot = None
        if bot:
            await bot.session.close()


async def run_once() -> bool:
    """Claim exclusive ownership; API cold starts must not recover active sends."""
    store = Store.from_environment("data/monitor.db", recover=False, require_remote=True)
    identifier = uuid.uuid4().hex
    monitor = None
    claimed = False
    try:
        claimed = store.acquire_lease(identifier)
        if not claimed:
            LOGGER.warning("Another scheduled collector owns the lease; skipping this invocation")
            return False
        store.recover_deliveries()
        monitor = Monitor(
            store, int(os.getenv("POLL_INTERVAL", str(DEFAULT_INTERVAL))),
            os.getenv("CITY24_ENABLED", "true").lower() == "true",
            os.getenv("LATIO_PERMISSION_CONFIRMED", "false").lower() == "true",
            os.getenv("RENTINRIGA_ENABLED", "true").lower() == "true",
        )
        store.set_setting("collector_run", {"started_at": now(), "expires_at": time.time() + 1500})
        await monitor.start(background=False)
        await asyncio.wait_for(collect(monitor), timeout=RUN_TIMEOUT)
        store.set_setting("last_scan", now())
        store.event("Scheduled check completed")
        return True
    except Exception:
        LOGGER.exception("Scheduled check failed")
        store.event("Scheduled check failed; inspect source status and GitHub Actions logs")
        raise
    finally:
        try:
            if monitor:
                await monitor.close()
        finally:
            try:
                if claimed:
                    store.set_setting("collector_run", {})
                    store.release_lease(identifier)
            finally:
                store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_once())
