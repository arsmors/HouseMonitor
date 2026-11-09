"""Verify libSQL result compatibility and preserve all storage invariants."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.database import RemoteConnection, batch, connect
from backend.store import Store
from tests import test_store


class RemoteStoreTests(test_store.StoreTests):
    """Run storage acceptance tests through the actual cross-platform SDK."""

    def connection(self, path):
        if path == ":memory:":
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            path = str(Path(directory.name) / "monitor.db")
        return sqlite3.connect(path)

    def setUp(self):
        def connect_remote(path, token=None):
            client = self.connection(path)
            self.addCleanup(client.close)
            return RemoteConnection(client)

        connection_patch = patch("backend.store.connect", side_effect=connect_remote)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        super().setUp()

    def test_schema_errors_are_not_silenced(self):
        with self.assertRaises(sqlite3.Error):
            self.store.db.executescript("CREATE TABLE invalid (")

    def test_lease_ownership_and_expiry(self):
        with patch("backend.store.time.time", return_value=100):
            self.assertTrue(self.store.acquire_lease("first", duration=20))
            self.assertFalse(self.store.acquire_lease("second"))
            self.store.release_lease("second")
            self.assertFalse(self.store.acquire_lease("second"))
        with patch("backend.store.time.time", return_value=121):
            self.assertTrue(self.store.acquire_lease("second"))
            self.store.release_lease("first")
            self.assertFalse(self.store.acquire_lease("third"))
            self.store.release_lease("second")
            self.assertTrue(self.store.acquire_lease("third"))

    def test_large_scope_uses_one_atomic_batch(self):
        listings = [test_store.listing(f"ss:{number}") for number in range(1000)]
        with patch.object(self.store.db, "batch", wraps=self.store.db.batch) as write:
            result = self.store.ingest("large", listings)
            self.assertEqual(write.call_count, 1)
        self.assertEqual(result["discovered"], 1000)
        self.assertEqual(result["queued"], 0)
        self.assertEqual(len(self.store.listings(10000)), 1000)

    def test_failed_batch_rolls_back_every_statement(self):
        with self.assertRaises(sqlite3.Error):
            batch(self.store.db, [
                ("INSERT INTO scopes VALUES(?,?)", ("rollback-test", "now")),
                ("INSERT INTO scopes VALUES(?,?)", ("rollback-test", "now")),
            ])
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM scopes WHERE id='rollback-test'").fetchone())

    def test_duplicate_ids_in_same_scan_queue_only_once(self):
        self.store.ingest("duplicates", [])
        result = self.store.ingest("duplicates", [test_store.listing("same"), test_store.listing("same")])
        self.assertEqual(result["discovered"], 1)
        self.assertEqual(result["queued"], 1)

    def test_discovered_locations_are_persisted_in_one_batch(self):
        record = {"id": "ss:test", "label": "Test area", "path": "test", "parent": "ss:", "group": "Test"}
        self.store.save_locations([record])
        self.assertEqual(self.store.locations()[record["id"]], record)
        updated = {**record, "label": "Updated area"}
        self.store.save_locations([updated])
        self.assertEqual(self.store.locations()[record["id"]], updated)
        self.store.save_locations([])


class ConfigurationTests(unittest.TestCase):
    def test_remote_schema_skips_local_only_pragmas(self):
        client = Mock()
        client.execute.return_value.description = None
        connection = RemoteConnection(client)
        connection.executescript(
            "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;"
            " PRAGMA busy_timeout=5000; CREATE TABLE example (id INTEGER);"
        )
        self.assertEqual(
            [call.args[0].strip() for call in client.execute.call_args_list],
            ["PRAGMA foreign_keys=ON", "CREATE TABLE example (id INTEGER)"],
        )

    def test_scheduled_storage_cannot_fall_back_to_local_disk(self):
        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
            with self.assertRaisesRegex(ValueError, "TURSO_DATABASE_URL"):
                Store.from_environment(":memory:", require_remote=True)
        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "libsql://example", "TURSO_AUTH_TOKEN": ""}):
            with self.assertRaisesRegex(ValueError, "TURSO_AUTH_TOKEN"):
                Store.from_environment(":memory:")

    def test_remote_connection_requires_token_and_correct_url(self):
        with self.assertRaisesRegex(ValueError, "TURSO_AUTH_TOKEN"):
            connect("libsql://example")
        with self.assertRaisesRegex(ValueError, "libsql://"):
            connect("https://example", auth_token="test-token")

    def test_remote_libsql_urls_use_libsql_driver(self):
        with patch("backend.database.import_module") as import_module:
            create_client = import_module.return_value.connect
            connection = connect("libsql://example.turso.io", auth_token="test-token")
        import_module.assert_called_once_with("libsql")
        create_client.assert_called_once_with(database="libsql://example.turso.io", auth_token="test-token")

    def test_scheduled_api_does_not_quarantine_live_deliveries(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "monitor.db")
            store = Store(path)
            store.ingest("scope", [])
            store.ingest("scope", [test_store.listing("new")])
            job = store.pending(0)[0]
            store.delivery(job["id"], "sending")
            store.close()
            api_store = Store(path, recover=False)
            try:
                self.assertEqual(api_store.status()["queue"]["sending"], 1)
                self.assertEqual(len(api_store.searches()), 1)
                api_store.recover_deliveries()
                self.assertEqual(api_store.status()["queue"]["uncertain"], 1)
            finally:
                api_store.close()
