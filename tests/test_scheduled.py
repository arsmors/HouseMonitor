"""Scheduled deployment tests without source requests or real Telegram messages."""

import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import encode_basic_auth
from aiohttp.test_utils import TestClient, TestServer

from backend.app import create_app
from backend.scheduled import collect, poll_commands, run_once
from backend.service import Monitor
from backend.store import Store


class ScheduledTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.monitor = Monitor(self.store)

    def tearDown(self):
        self.store.close()

    async def test_dashboard_starts_no_workers_and_reports_shared_state(self):
        self.store.set_setting("last_scan", "2026-11-09T10:00:00+00:00")
        self.store.set_setting("collector_run", {"started_at": "2026-11-09T11:00:00+00:00", "expires_at": time.time() + 120})
        with patch.dict(os.environ, {"MONITOR_MODE": "scheduled", "WEB_PASSWORD": "test-password"}):
            client = TestClient(TestServer(create_app(self.store)))
        with patch.object(self.store, "close") as close:
            await client.start_server()
            try:
                self.assertEqual(client.server.app["monitor"].tasks, [])
                self.assertIsNotNone(client.server.app["monitor"].ss)
                auth = {"Authorization": encode_basic_auth("owner", "test-password")}
                response = await client.get("/api/state", headers=auth)
                status = (await response.json())["status"]
                self.assertEqual(status["mode"], "scheduled")
                self.assertTrue(status["scanning"])
                self.assertEqual(status["last_scan"], "2026-11-09T10:00:00+00:00")
                self.assertIsNone(status["next_scan"])
                response = await client.post("/api/scan", headers={**auth, "X-House-Monitor": "1"})
                self.assertEqual(response.status, 409)
                self.assertIn("GitHub Actions", (await response.json())["error"])
            finally:
                await client.close()
            close.assert_called_once()

    def test_expired_scan_is_not_reported_as_running(self):
        self.monitor.scheduled = True
        self.store.set_setting("collector_run", {"expires_at": time.time() - 1})
        self.assertFalse(self.monitor.status()["scanning"])
        self.assertFalse(self.monitor.trigger())

    def test_scheduled_dashboard_requires_password(self):
        with patch.dict(os.environ, {"MONITOR_MODE": "scheduled", "WEB_PASSWORD": "", "PUBLIC_ACCESS": "false"}):
            with self.assertRaisesRegex(ValueError, "WEB_PASSWORD"):
                create_app(self.store)

    async def test_public_dashboard_allows_remote_reads_and_changes(self):
        with patch.dict(os.environ, {"MONITOR_MODE": "scheduled", "WEB_PASSWORD": "", "PUBLIC_ACCESS": "true"}):
            client = TestClient(TestServer(create_app(self.store, start_tasks=False)))
        await client.start_server()
        try:
            headers = {"Host": "monitor.example", "X-House-Monitor": "1", "Origin": "https://monitor.example"}
            response = await client.get("/api/state", headers=headers)
            self.assertEqual(response.status, 200)
            response = await client.put("/api/settings", json={"notifications_enabled": False}, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertFalse(self.store.setting("notifications_enabled"))
            response = await client.post("/api/searches", json={"name": "Public search", "locations": []}, headers=headers)
            self.assertEqual(response.status, 201)
            identifier = (await response.json())["id"]
            response = await client.delete(f"/api/searches/{identifier}", headers=headers)
            self.assertEqual(response.status, 200)
            response = await client.put("/api/settings", json={"notifications_enabled": True}, headers={**headers, "Origin": "https://evil.example"})
            self.assertEqual(response.status, 403)
            response = await client.put("/api/settings", json={"notifications_enabled": True}, headers={"Host": "monitor.example"})
            self.assertEqual(response.status, 403)
        finally:
            await client.close()

    async def test_public_flag_does_not_override_configured_password(self):
        with patch.dict(os.environ, {"MONITOR_MODE": "scheduled", "WEB_PASSWORD": "test-password", "PUBLIC_ACCESS": "true"}):
            client = TestClient(TestServer(create_app(self.store, start_tasks=False)))
        await client.start_server()
        try:
            response = await client.get("/api/state", headers={"Host": "monitor.example"})
            self.assertEqual(response.status, 401)
        finally:
            await client.close()

    async def test_one_run_closes_resources_and_releases_lease(self):
        with (patch("backend.scheduled.Store.from_environment", return_value=self.store) as storage,
              patch("backend.scheduled.Monitor.start", new_callable=AsyncMock) as start,
              patch("backend.scheduled.Monitor.close", new_callable=AsyncMock) as close,
              patch("backend.scheduled.collect", new_callable=AsyncMock) as collector,
              patch.object(self.store, "close") as database_close):
            self.assertTrue(await run_once())
            storage.assert_called_once_with("data/monitor.db", recover=False, require_remote=True)
            start.assert_awaited_once_with(background=False)
            collector.assert_awaited_once()
            close.assert_awaited_once()
            database_close.assert_called_once()
        self.assertIsNotNone(self.store.setting("last_scan"))
        self.assertEqual(self.store.setting("collector_run"), {})
        self.assertTrue(self.store.acquire_lease("next"))

    async def test_failed_run_is_not_reported_as_success(self):
        with (patch("backend.scheduled.Store.from_environment", return_value=self.store),
              patch("backend.scheduled.Monitor.start", new_callable=AsyncMock),
              patch("backend.scheduled.Monitor.close", new_callable=AsyncMock),
              patch("backend.scheduled.collect", new_callable=AsyncMock, side_effect=RuntimeError("test failure")),
              patch.object(self.store, "close")):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                await run_once()
        self.assertIsNone(self.store.setting("last_scan"))
        self.assertEqual(self.store.setting("collector_run"), {})
        self.assertTrue(self.store.acquire_lease("next"))

    async def test_overlapping_run_does_not_recover_or_collect(self):
        self.store.acquire_lease("existing")
        with (patch("backend.scheduled.Store.from_environment", return_value=self.store),
              patch("backend.scheduled.collect", new_callable=AsyncMock) as collector,
              patch.object(self.store, "recover_deliveries") as recover,
              patch.object(self.store, "close")):
            self.assertFalse(await run_once())
            recover.assert_not_called()
            collector.assert_not_awaited()
        self.assertFalse(self.store.acquire_lease("next"))

    async def test_collection_without_telegram_is_bounded_and_runs_once(self):
        self.monitor.cycle = AsyncMock()
        self.monitor.enrich_images = AsyncMock()
        self.monitor.deliver = AsyncMock()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_OWNER_ID": ""}):
            await collect(self.monitor)
        self.monitor.cycle.assert_awaited_once()
        self.monitor.enrich_images.assert_awaited_once()
        self.monitor.deliver.assert_not_awaited()
        self.assertFalse(self.store.setting("scheduled_telegram")["configured"])

    async def test_failed_source_error_is_reported_after_other_work(self):
        self.store.source_state("RentInRiga.lv", {
            "status": "error", "error": "Cannot verify www.rentinriga.lv robots.txt (403)"
        })
        self.monitor.cycle = AsyncMock()
        self.monitor.enrich_images = AsyncMock()
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_OWNER_ID": ""}):
            with self.assertRaisesRegex(RuntimeError, r"RentInRiga.lv: Cannot verify www.rentinriga.lv robots.txt \(403\)"):
                await collect(self.monitor)
        self.monitor.enrich_images.assert_awaited_once()

    async def test_scheduled_telegram_owner_only_and_durable_offset(self):
        self.monitor.bot = AsyncMock()
        self.monitor.owner_id = 123
        self.store.set_setting("telegram_offset", 10)

        def update(identifier, owner, text):
            return SimpleNamespace(update_id=identifier, message=SimpleNamespace(
                from_user=SimpleNamespace(id=owner),
                chat=SimpleNamespace(id=owner, type="private"), text=text,
            ))

        self.monitor.bot.get_updates.return_value = [update(10, 999, "/start"), update(11, 123, "/start")]
        await poll_commands(self.monitor)
        self.monitor.bot.get_updates.assert_awaited_once_with(offset=10, timeout=0, limit=100)
        self.monitor.bot.send_message.assert_awaited_once()
        self.assertEqual(self.monitor.bot.send_message.call_args.args[0], 123)
        self.assertTrue(self.store.setting("telegram_ready"))
        self.assertEqual(self.store.setting("telegram_offset"), 12)
