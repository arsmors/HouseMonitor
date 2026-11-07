"""Telegram delivery and authorization checks without external messages."""

import unittest
import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User

from backend.service import Monitor, interval_label, notification_text
from backend.sources import Listing, close_browser
from backend.store import Store
from backend.telegram import OwnerOnly


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    """Exercise durable send outcomes and owner-only access."""

    def setUp(self):
        """Seed one due notification in an isolated outbox."""
        self.store = Store(":memory:")
        self.monitor = Monitor(self.store, rentinriga=False)
        self.monitor.owner_id = 123
        self.monitor.bot = AsyncMock()
        self.monitor.bot.send_message.return_value = SimpleNamespace(message_id=42)
        self.store.ingest("scope", [])
        self.store.ingest("scope", [Listing("ss:new", "SS.lv", "<script>House</script>", "https://www.ss.lv/msg/house.html", "100 €", "Vecāķi", ["ss:riga/vecaki"], None, "", "missing")])

    def tearDown(self):
        """Close test persistence."""
        self.store.close()

    def test_hourly_default_interval(self):
        """Use an hourly default and keep configurable human-readable intervals."""
        self.assertEqual(self.monitor.interval, 3600)
        self.assertEqual(interval_label(self.monitor.interval), "1 hour")
        self.assertEqual(interval_label(7200), "2 hours")
        self.assertEqual(interval_label(3600), "1 hour")
        self.assertEqual(interval_label(300), "5 minutes")

    async def test_send_once_and_escape(self):
        """Send one escaped message and never resend a completed job."""
        await self.monitor.deliver()
        await self.monitor.deliver()
        self.monitor.bot.send_message.assert_awaited_once()
        text = self.monitor.bot.send_message.call_args.args[1]
        self.assertIn("&lt;script&gt;", text)
        self.assertEqual(self.store.status()["queue"]["sent"], 1)

    async def test_rate_limit_stays_retryable(self):
        """A confirmed Telegram rate-limit rejection remains a due-later job."""
        self.monitor.bot.send_message.side_effect = TelegramRetryAfter(SendMessage(chat_id=123, text="x"), "retry", retry_after=10)
        await self.monitor.deliver()
        self.assertEqual(self.store.status()["queue"]["failed"], 1)

    async def test_ambiguous_network_send_not_duplicated(self):
        """An unconfirmed send is quarantined instead of retried blindly."""
        self.monitor.bot.send_message.side_effect = TelegramNetworkError(SendMessage(chat_id=123, text="x"), "timeout")
        await self.monitor.deliver()
        await self.monitor.deliver()
        self.monitor.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.status()["queue"]["uncertain"], 1)

    async def test_owner_authorization(self):
        """Deny a different sender before executing any handler."""
        handler = AsyncMock()
        middleware = OwnerOnly(123)
        message = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=999, type="private"), from_user=User(id=999, is_bot=False, first_name="Other"), text="/delete 1")
        await middleware(handler, message, {})
        handler.assert_not_awaited()
        owner_message = message.model_copy(update={"chat": Chat(id=123, type="private"), "from_user": User(id=123, is_bot=False, first_name="Owner")})
        await middleware(handler, owner_message, {})
        handler.assert_awaited_once()

    async def test_image_worker_only_upgrades_matches_once(self):
        """Backfill matching photos once without changing notification queue state."""
        self.store.ingest("scope", [Listing("ss:outside", "SS.lv", "Outside", "https://www.ss.lv/msg/outside.html", "100 €", "Mārupe", ["ss:riga-region/marupes-pag"], 165, "165", "parsed")])
        self.monitor.ss = SimpleNamespace(full_image=AsyncMock(return_value="https://i.ss.lv/gallery/house.800.jpg"))
        before = self.store.status()["queue"]
        await self.monitor.enrich_images()
        await self.monitor.enrich_images()
        self.monitor.ss.full_image.assert_awaited_once_with("https://www.ss.lv/msg/house.html")
        upgraded = next(listing for listing in self.store.listings() if listing["id"] == "ss:new")
        self.assertEqual(upgraded["image"], "https://i.ss.lv/gallery/house.800.jpg")
        self.assertEqual(self.store.status()["queue"], before)

    async def test_image_failure_keeps_listing_and_thumbnail(self):
        """A blocked detail page does not hide a listing or fail the monitor."""
        self.monitor.ss = SimpleNamespace(full_image=AsyncMock(side_effect=ValueError("Source unavailable")))
        await self.monitor.enrich_images()
        self.assertEqual(len(self.store.listings()), 1)
        self.assertIsNone(self.store.listings()[0]["image"])
        self.assertIn("image_checked_at", self.store.listings()[0])

    async def test_timeout_clears_checking_and_keeps_listings(self):
        """A stuck adapter becomes an error without deleting the last complete results."""
        self.store.set_setting("catalog_loaded", True)
        self.monitor.ss = SimpleNamespace(collect=AsyncMock(return_value=[]), locations=self.store.locations())
        before = len(self.store.listings())
        blocked = asyncio.Event()
        with patch("backend.service.SOURCE_TIMEOUT", 0.01), patch("backend.service.City24Adapter.collect", new=AsyncMock(side_effect=blocked.wait)):
            await self.monitor.cycle()
        sources = self.store.status()["sources"]
        self.assertEqual(sources["City24.lv"]["status"], "error")
        self.assertIn("timed out", sources["City24.lv"]["error"])
        self.assertFalse(any(state["status"] == "scanning" for state in sources.values()))
        self.assertEqual(len(self.store.listings()), before)

    async def test_browser_cleanup_is_bounded(self):
        """Hung browser shutdown cannot keep a source scan pending indefinitely."""
        blocked = asyncio.Event()
        browser = SimpleNamespace(close=AsyncMock(side_effect=blocked.wait))
        driver = SimpleNamespace(stop=AsyncMock(side_effect=blocked.wait))
        await asyncio.wait_for(close_browser(browser, driver, timeout=0.01), timeout=1)
        browser.close.assert_awaited_once()
        driver.stop.assert_awaited_once()

    async def test_scheduler_waits_and_preserves_next_due(self):
        """Respect a saved next scan and run once only after an explicit manual trigger."""
        self.monitor.next_scan = time.time() + self.monitor.interval
        finished = asyncio.Event()
        self.monitor.cycle = AsyncMock(side_effect=finished.set)
        task = asyncio.create_task(self.monitor.run())
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(finished.wait(), timeout=0.01)
            self.monitor.cycle.assert_not_awaited()
            self.assertTrue(self.monitor.trigger())
            await asyncio.wait_for(finished.wait(), timeout=1)
            self.monitor.cycle.assert_awaited_once()
            self.assertFalse(self.monitor.scanning)
            self.assertGreater(self.store.setting("next_scan"), time.time() + self.monitor.interval - 100)
            restarted = Monitor(self.store)
            self.assertEqual(restarted.next_scan, self.monitor.next_scan)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def test_shorter_interval_adjusts_saved_next_check(self):
        """An old two-hour deadline must not delay a newly hourly configuration."""
        self.store.set_setting("last_scan", "2026-11-05T12:00:00+00:00")
        self.store.set_setting("next_scan", datetime(2026, 11, 5, 14, tzinfo=timezone.utc).timestamp())
        monitor = Monitor(self.store)
        self.assertEqual(monitor.next_scan, datetime(2026, 11, 5, 13, tzinfo=timezone.utc).timestamp())
        self.assertEqual(self.store.setting("next_scan"), monitor.next_scan)

    async def test_rentinriga_source_baseline_and_deduplication(self):
        """New provider matches are stored silently on baseline and not queued twice."""
        self.store.set_setting("catalog_loaded", True)
        self.monitor.city24_enabled = False
        self.monitor.rentinriga_enabled = True
        self.monitor.ss = SimpleNamespace(collect=AsyncMock(return_value=[]), locations=self.store.locations())
        house = Listing("rentinriga:63973", "RentInRiga.lv", "Lilaste 40A", "https://www.rentinriga.lv/lv/maju-pardosana/lilaste-40a/63973", "330 000 €", "Lilaste, Carnikavas pag.", ["ss:riga-region/carnikavas-nov"], 191, "191 m²", "parsed")
        before = self.store.status()["queue"]
        with patch("backend.service.RentInRigaAdapter.collect", new=AsyncMock(return_value=[house])):
            await self.monitor.cycle()
            await self.monitor.cycle()
        source = self.store.status()["sources"]["RentInRiga.lv"]
        self.assertEqual(source["status"], "ok")
        self.assertEqual(source["total"], 1)
        self.assertEqual(source["queued"], 0)
        self.assertEqual(source["discovered"], 0)
        self.assertEqual(self.store.status()["queue"], before)
        self.assertTrue(any(listing["id"] == house.id for listing in self.store.listings()))

    async def test_sources_fail_independently(self):
        """Keep successful SS.lv state when City24 fails and Latio is disabled."""
        self.store.set_setting("catalog_loaded", True)
        self.monitor.ss = SimpleNamespace(collect=AsyncMock(return_value=[]), locations=self.store.locations())
        with patch("backend.service.City24Adapter.collect", new=AsyncMock(side_effect=ValueError("Source unavailable"))):
            await self.monitor.cycle()
        sources = self.store.status()["sources"]
        self.assertEqual(sources["SS.lv"]["status"], "ok")
        self.assertEqual(sources["City24.lv"]["status"], "error")
        self.assertEqual(sources["Latio.lv"]["status"], "permission_required")

    async def test_no_searches_collects_default_areas_without_alerts(self):
        """Deleting searches retains default-area collection but queues no alerts."""
        from backend.sources import RECENT_LOCATION_IDS
        for search in self.store.searches():
            self.store.delete_search(search["id"])
        self.store.set_setting("catalog_loaded", True)
        self.monitor.city24_enabled = False
        self.monitor.ss = SimpleNamespace(collect=AsyncMock(return_value=[]), locations=self.store.locations())
        await self.monitor.cycle()
        self.assertEqual({call.args[0] for call in self.monitor.ss.collect.await_args_list}, {location.removeprefix("ss:") for location in RECENT_LOCATION_IDS})
        self.assertEqual(self.store.status()["sources"]["SS.lv"]["status"], "ok")
        self.assertEqual(self.store.pending(0), [])


if __name__ == "__main__":
    unittest.main()