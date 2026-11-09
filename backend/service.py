"""Monitoring lifecycle and durable Telegram notification delivery."""

import asyncio
import html
import logging
import time
from datetime import datetime

import aiohttp
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from backend.sources import RECENT_LOCATION_IDS, City24Adapter, LatioAdapter, PublicHTTP, RentInRigaAdapter, SSAdapter, USER_AGENT, is_sale_listing, match, now
from backend.store import Store

LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL = 3600
SOURCE_TIMEOUT = 240


def interval_label(seconds: int) -> str:
    """Display configured polling intervals in hours when possible."""
    amount, unit = (seconds // 3600, "hour") if seconds % 3600 == 0 else (seconds // 60, "minute")
    return f'{amount} {unit}{"" if amount == 1 else "s"}'


def notification_text(listing: dict, search: dict) -> str:
    """Escape untrusted source text before formatting a Telegram message."""
    area = f'{listing["area"]:g} m²' if listing["area"] is not None else "Not provided"
    publication = f'\nPublished: {html.escape(listing["published_at"])}' if listing.get("published_at") else ""
    return (f'<b>New house on {html.escape(listing["source"])}</b>\n\n'
            f'<b>{html.escape(listing["title"][:220])}</b>\n'
            f'{html.escape(listing["price"])}\n'
            f'Location: {html.escape(listing["location"])}\nHouse area: {area}{publication}\n\n'
            f'Search: {html.escape(search["name"])}')


class Monitor:
    """Coordinate source scans without overlaps and isolate source failures."""

    def __init__(self, store: Store, interval: int = DEFAULT_INTERVAL, city24: bool = True, latio: bool = False, rentinriga: bool = True):
        """Configure independent sources with an hourly default schedule."""
        self.store = store
        self.interval = max(interval, 300)
        self.city24_enabled = city24
        self.latio_enabled = latio
        self.rentinriga_enabled = rentinriga
        self.bot = None
        self.owner_id = None
        self.bot_username = None
        self.bot_error = None
        self.scanning = False
        self.scan_started_at = None
        self.last_scan = self.store.setting("last_scan")
        self.next_scan = self.store.setting("next_scan")
        if self.last_scan and self.next_scan:
            next_due = datetime.fromisoformat(self.last_scan).timestamp() + self.interval
            if self.next_scan > next_due:
                self.next_scan = next_due
                self.store.set_setting("next_scan", next_due)
        if self.next_scan is None:
            successes = [state["last_success"] for state in self.store.status()["sources"].values() if state.get("last_success")]
            if successes:
                self.next_scan = max(datetime.fromisoformat(timestamp).timestamp() for timestamp in successes) + self.interval
                self.last_scan = self.last_scan or max(successes)
        self.session = None
        self.ss = None
        self.http = None
        self.tasks: list[asyncio.Task] = []
        self.wake = asyncio.Event()
        self.location_lock = asyncio.Lock()
        self.last_trigger = 0.0
        self.scheduled = False

    async def start(self, background: bool = True):
        """Open shared resources and schedule scans/delivery in the background."""
        for source, state in self.store.status()["sources"].items():
            if state.get("status") == "scanning":
                self.store.source_state(source, {**state, "status": "interrupted", "error": "Previous check was interrupted. Existing listings retained; next scheduled check will retry."})
        self.session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=45))
        self.http = PublicHTTP(self.session)
        self.ss = SSAdapter(self.http, self.store.locations())
        if background:
            self.tasks = [asyncio.create_task(self.run(), name="monitor"), asyncio.create_task(self.deliver_loop(), name="outbox"), asyncio.create_task(self.image_loop(), name="listing-photos")]

    async def close(self):
        """Cancel work and explicitly release HTTP resources."""
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.session:
            await self.session.close()

    async def discover(self, path: str = "") -> list[dict]:
        """Serialize discovery to avoid duplicate source requests."""
        async with self.location_lock:
            records = await self.ss.discover(path)
            self.store.save_locations(records)
            return records

    async def bootstrap(self):
        """Discover source regions and the requested area's actual hierarchy."""
        if not self.store.setting("catalog_loaded"):
            for path in ["", "riga", "riga-region", "riga-region/carnikavas-nov"]:
                await self.discover(path)
            self.store.set_setting("catalog_loaded", True)

    def trigger(self) -> bool:
        """Allow a user scan without permitting rapid repeated source requests."""
        if self.scheduled:
            return False
        if self.scanning or time.monotonic() - self.last_trigger < 60:
            return False
        self.last_trigger = time.monotonic()
        self.wake.set()
        return True

    async def run(self):
        """Persist the next due time and never restart an overlong scan in a tight loop."""
        while True:
            if self.next_scan and self.next_scan > time.time() and not self.wake.is_set():
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=self.next_scan - time.time())
                except asyncio.TimeoutError:
                    pass
            self.wake.clear()
            self.scanning = True
            self.scan_started_at = now()
            self.next_scan = time.time() + self.interval
            self.store.set_setting("next_scan", self.next_scan)
            try:
                await asyncio.wait_for(self.cycle(), timeout=SOURCE_TIMEOUT * 4 + 30)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.error("Monitoring cycle failed: %s", type(error).__name__)
                self.store.event("Monitoring cycle failed; see source status")
            finally:
                self.scanning = False
                self.scan_started_at = None
                self.last_scan = now()
                self.store.set_setting("last_scan", self.last_scan)
                if self.next_scan <= time.time():
                    self.next_scan = time.time() + self.interval
                    self.store.set_setting("next_scan", self.next_scan)
                for source, state in self.store.status()["sources"].items():
                    if state.get("status") == "scanning":
                        self.store.source_state(source, {**state, "status": "interrupted", "error": "Check did not complete. Existing listings retained; next scheduled check will retry."})

    async def collect_ss(self, active: list[dict]) -> dict:
        """Collect SS.lv scopes under the same deadline as the other sources."""
        await self.bootstrap()
        totals = {"total": 0, "discovered": 0, "queued": 0}
        paths = {location.removeprefix("ss:") for search in active for location in search["locations"]}
        if any(not search["locations"] for search in active):
            paths = {""}
        paths = {path for path in paths if not any(path != parent and path.startswith(parent + "/") for parent in paths)}
        for path in sorted(paths):
            result = self.store.ingest("ss:" + path, await self.ss.collect(path))
            for key in totals:
                totals[key] += result[key]
        return totals

    async def cycle(self):
        """Collect SS.lv first, then independent additional sources."""
        searches = self.store.searches()
        active = [search for search in searches if search["active"]]
        if not searches:
            active = [{"locations": RECENT_LOCATION_IDS, "min": None, "max": None}]
        if not active:
            self.store.event("Monitoring idle: all searches are paused")
            return
        city_locations = None if any(not search["locations"] for search in active) else sorted({location for search in active for location in search["locations"]})
        city_scope = "City24.lv" if city_locations is None else "City24.lv:" + "|".join(city_locations)
        rent_scope = "RentInRiga.lv" if city_locations is None else "RentInRiga.lv:" + "|".join(city_locations)
        for source in ["SS.lv", "City24.lv", "RentInRiga.lv", "Latio.lv"]:
            if source == "City24.lv" and not self.city24_enabled:
                self.store.source_state(source, {"status": "disabled", "error": "Disabled in configuration"})
                continue
            if source == "RentInRiga.lv" and not self.rentinriga_enabled:
                self.store.source_state(source, {"status": "disabled", "error": "Disabled in configuration"})
                continue
            if source == "Latio.lv" and not self.latio_enabled:
                self.store.source_state(source, {"status": "permission_required", "error": "Written reuse permission required. Set LATIO_PERMISSION_CONFIRMED=true only after obtaining permission."})
                continue
            previous = self.store.status()["sources"].get(source, {})
            changed_scope = source == "City24.lv" and previous.get("scope") != city_scope or source == "RentInRiga.lv" and previous.get("scope") != rent_scope
            if previous.get("retry_at", 0) > time.time() and not changed_scope:
                continue
            started = time.monotonic()
            self.store.source_state(source, {**previous, "status": "scanning", "started_at": now(), "timeout_seconds": SOURCE_TIMEOUT})
            try:
                totals = {"total": 0, "discovered": 0, "queued": 0}
                detail = {}
                if source == "SS.lv":
                    totals = await asyncio.wait_for(self.collect_ss(active), timeout=SOURCE_TIMEOUT)
                elif source == "City24.lv":
                    cached = self.store.setting("city24_search_urls", {})
                    adapter = City24Adapter(self.http, self.ss.locations, city_locations, cached.get(city_scope))
                    listings = await asyncio.wait_for(adapter.collect(), timeout=SOURCE_TIMEOUT)
                    totals = self.store.ingest(city_scope, listings)
                    if adapter.search_urls:
                        cached[city_scope] = adapter.search_urls
                        self.store.set_setting("city24_search_urls", cached)
                    detail = {"scope": city_scope, "pages_checked": adapter.pages_checked, "search_urls": adapter.search_urls}
                elif source == "RentInRiga.lv":
                    cached = self.store.setting("rentinriga_search_urls", {})
                    adapter = RentInRigaAdapter(self.http, self.ss.locations, city_locations, cached.get(rent_scope))
                    listings = await asyncio.wait_for(adapter.collect(), timeout=SOURCE_TIMEOUT)
                    totals = self.store.ingest(rent_scope, listings)
                    cached[rent_scope] = adapter.search_urls
                    self.store.set_setting("rentinriga_search_urls", cached)
                    detail = {"scope": rent_scope, "pages_checked": adapter.pages_checked, "search_urls": adapter.search_urls}
                else:
                    adapter = LatioAdapter(self.http, self.ss.locations)
                    totals = self.store.ingest(source, await asyncio.wait_for(adapter.collect(), timeout=SOURCE_TIMEOUT))
                self.store.source_state(source, {"status": "ok", "last_success": now(), "failures": 0, "duration_seconds": round(time.monotonic() - started, 1), **totals, **detail})
                self.store.event(f'{source}: {totals["total"]} listings checked, {totals["discovered"]} discovered, {totals["queued"]} notifications queued')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures = previous.get("failures", 0) + 1
                reason = str(error)[:250] if isinstance(error, ValueError) else type(error).__name__ + ": public source could not be collected"
                if isinstance(error, asyncio.TimeoutError):
                    reason = f"Source check timed out after {SOURCE_TIMEOUT:g} seconds. Existing listings retained."
                state = {**previous, "status": "error", "error": reason, "failures": failures, "retry_at": time.time() + min(self.interval * 2 ** min(failures - 1, 6), max(self.interval, 3600))}
                if source == "City24.lv":
                    state["scope"] = city_scope
                elif source == "RentInRiga.lv":
                    state["scope"] = rent_scope
                self.store.source_state(source, state)
                self.store.event(f"{source}: {reason}")
                LOGGER.warning("%s collection failed: %s", source, type(error).__name__)
                if failures >= 3 and not previous.get("alerted") and self.bot and self.store.setting("telegram_ready"):
                    try:
                        await self.bot.send_message(self.owner_id, f"Monitoring warning: {source} has failed {failures} times. Other sources continue. Check /status.")
                        self.store.source_state(source, {**state, "alerted": True})
                    except Exception:
                        LOGGER.warning("Could not deliver monitoring warning")

    async def enrich_images(self):
        """Upgrade visible and matching SS.lv photos with a persistent one-day cache."""
        active = [search for search in self.store.searches() if search["active"]]
        recent_locations = set(RECENT_LOCATION_IDS)
        for listing in self.store.listings(10000):
            visible = bool(recent_locations.intersection(listing["location_ids"]))
            matched = any(match(listing, search) for search in active)
            if listing["source"] != "SS.lv" or not visible and not matched:
                continue
            if time.time() - listing.get("image_checked_at", 0) < 86400:
                continue
            try:
                image = await self.ss.full_image(listing["url"])
                self.store.update_image(listing["id"], image, time.time())
            except (ValueError, aiohttp.ClientError, asyncio.TimeoutError):
                self.store.update_image(listing["id"], None, time.time())
                LOGGER.debug("SS.lv photo unavailable; keeping existing image")
                break

    async def image_loop(self):
        """Backfill existing photos independently of scanning and notification delivery."""
        while True:
            try:
                await self.enrich_images()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning("Photo enrichment failed: %s", type(error).__name__)
            await asyncio.sleep(30)

    async def deliver_loop(self):
        """Drain due outbox work independently of slow source scans."""
        while True:
            try:
                if self.bot and self.store.setting("telegram_ready"):
                    await self.deliver()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning("Outbox iteration failed: %s", type(error).__name__)
            await asyncio.sleep(30)

    async def deliver(self):
        """Send messages with durable state and explicit handling of ambiguous outcomes."""
        searches = {search["id"]: search for search in self.store.searches()}
        for job in self.store.pending(time.time()):
            row = self.store.db.execute("SELECT data FROM listings WHERE id=?", (job["listing_id"],)).fetchone()
            if not row:
                continue
            import json
            listing = json.loads(row[0])
            if not is_sale_listing(listing):
                self.store.delivery(job["id"], "cancelled", "Buyer request is not a sale listing")
                continue
            search = searches.get(job["search_id"])
            if not search or not match(listing, search):
                self.store.delivery(job["id"], "cancelled", "No longer matches edited search")
                continue
            self.store.delivery(job["id"], "sending")
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Open original advertisement", url=listing["url"])]])
            try:
                if listing.get("image"):
                    try:
                        message = await self.bot.send_photo(self.owner_id, photo=listing["image"], caption=notification_text(listing, search), parse_mode="HTML", reply_markup=keyboard)
                    except TelegramBadRequest:
                        message = await self.bot.send_message(self.owner_id, notification_text(listing, search), parse_mode="HTML", reply_markup=keyboard)
                else:
                    message = await self.bot.send_message(self.owner_id, notification_text(listing, search), parse_mode="HTML", reply_markup=keyboard)
                self.store.delivery(job["id"], "sent", message_id=message.message_id)
            except TelegramRetryAfter as error:
                self.store.delivery(job["id"], "failed", "Telegram rate limit", time.time() + error.retry_after)
                break
            except (TelegramForbiddenError, TelegramBadRequest) as error:
                self.store.delivery(job["id"], "failed", type(error).__name__, time.time() + min(60 * 2 ** min(job["attempts"], 6), 3600))
            except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError):
                self.store.delivery(job["id"], "uncertain", "Telegram response not confirmed. Inspect chat before retrying to avoid duplicate alerts.")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.store.delivery(job["id"], "uncertain", type(error).__name__)
            await asyncio.sleep(0.1)

    def status(self) -> dict:
        """Return runtime state without ever exposing tokens or passwords."""
        if self.scheduled:
            run = self.store.setting("collector_run", {})
            scanning = run.get("expires_at", 0) > time.time()
            return {**self.store.status(), "mode": "scheduled", "scanning": scanning,
                    "scan_started_at": run.get("started_at") if scanning else None,
                    "last_scan": self.store.setting("last_scan"), "next_scan": None, "interval": self.interval,
                    "telegram": self.store.setting("scheduled_telegram", {
                        "configured": False, "ready": False, "username": None,
                        "error": "Telegram connects during scheduled checks",
                    })}
        return {**self.store.status(), "scanning": self.scanning, "scan_started_at": self.scan_started_at, "last_scan": self.last_scan,
                "next_scan": self.next_scan, "interval": self.interval,
                "telegram": {"configured": bool(self.bot), "ready": bool(self.bot and self.store.setting("telegram_ready")),
                             "username": self.bot_username, "error": self.bot_error}}