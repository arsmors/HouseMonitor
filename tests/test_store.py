"""Persistence, baseline, queue and search lifecycle acceptance tests."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.sources import Listing
from backend.store import Store


def listing(identifier: str, area: float | None = 165) -> Listing:
    """Build a representative normalized listing without network access."""
    return Listing(identifier, "SS.lv", "House", "https://www.ss.lv/msg/house.html", "100,000 €", "Vecāķi", ["ss:riga/vecaki"], area, str(area), "parsed" if area else "missing")


class StoreTests(unittest.TestCase):
    """Verify transactional new-ad detection and restart behavior."""

    def setUp(self):
        """Create an isolated database for each test."""
        self.store = Store(":memory:")

    def tearDown(self):
        """Close the test database."""
        self.store.close()

    def test_baseline_duplicates_and_queue(self):
        """First scan stays quiet, new IDs queue once, overlapping scopes stay quiet."""
        self.assertEqual(self.store.ingest("ss:vecaki", [listing("old")])["queued"], 0)
        self.assertEqual(self.store.ingest("ss:vecaki", [listing("old"), listing("new")])["queued"], 1)
        self.assertEqual(self.store.ingest("ss:vecaki", [listing("new")])["queued"], 0)
        self.assertEqual(self.store.ingest("ss:all", [listing("new")])["queued"], 0)
        job = self.store.pending(0)[0]
        self.store.delivery(job["id"], "failed", "Temporary failure", 50)
        self.assertEqual(self.store.pending(49), [])
        self.assertEqual(len(self.store.pending(50)), 1)
        self.store.delivery(job["id"], "sent", message_id=123)
        self.assertEqual(self.store.pending(100), [])

    def test_pause_resume_edit_and_history(self):
        """Editing keeps history and paused searches cannot send queued work."""
        self.store.ingest("ss:vecaki", [listing("old")])
        self.store.ingest("ss:vecaki", [listing("new")])
        search = self.store.searches()[0]
        search["active"] = False
        self.store.save_search(search, search["id"])
        self.assertEqual(self.store.pending(100), [])
        search["active"] = True
        search["min"] = 100
        edited = self.store.save_search(search, search["id"])
        self.assertEqual(edited["count"], 2)
        self.assertEqual(len(self.store.pending(100)), 1)
        self.store.delete_search(search["id"])
        self.assertEqual(self.store.pending(100), [])
        self.assertEqual(len(self.store.listings()), 2)

    def test_restart_and_uncertain_delivery(self):
        """Persist state and quarantine interrupted sends rather than duplicate them."""
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "monitor.db")
            store = Store(path)
            store.ingest("scope", [])
            store.ingest("scope", [listing("new")])
            job = store.pending(0)[0]
            store.delivery(job["id"], "sending")
            store.close()
            reopened = Store(path)
            self.assertEqual(len(reopened.searches()), 1)
            self.assertEqual(reopened.pending(100), [])
            self.assertEqual(reopened.status()["queue"]["uncertain"], 1)
            reopened.close()

    def test_image_enrichment_survives_scans(self):
        """Keep a cached detail photo until the source thumbnail changes."""
        original = listing("house")
        original.image = "https://i.ss.lv/gallery/house.th2.jpg"
        self.store.ingest("scope", [original])
        first_seen = self.store.listings()[0]["first_seen_at"]
        self.store.update_image("house", "https://i.ss.lv/gallery/house.800.jpg", 12345)
        result = self.store.ingest("scope", [original])
        record = self.store.listings()[0]
        self.assertEqual(record["image"], "https://i.ss.lv/gallery/house.800.jpg")
        self.assertEqual(record["image_checked_at"], 12345)
        self.assertEqual(record["first_seen_at"], first_seen)
        self.assertEqual(result["queued"], 0)
        original.image = "https://i.ss.lv/gallery/replacement.th2.jpg"
        self.store.ingest("scope", [original])
        self.assertNotIn("image_checked_at", self.store.listings()[0])

    def test_new_ads_from_every_portal_sort_first(self):
        """Mix sources by discovery time, without moving refreshed or enriched old ads up."""
        old = listing("ss:old")
        fresh_ss = listing("ss:new")
        fresh_city = listing("city24:new")
        fresh_city.source = "City24.lv"
        fresh_latio = listing("latio:new")
        fresh_latio.source = "Latio.lv"
        with patch("backend.store.now", return_value="2026-11-05T10:00:00+00:00"):
            self.store.ingest("SS.lv", [old])
            self.store.ingest("City24.lv", [])
            self.store.ingest("Latio.lv", [])
        for scope, new, timestamp in [("SS.lv", fresh_ss, "10:01:00"), ("City24.lv", fresh_city, "10:02:00"), ("Latio.lv", fresh_latio, "10:03:00")]:
            with patch("backend.store.now", return_value=f"2026-11-05T{timestamp}+00:00"):
                self.store.ingest(scope, [new])
            self.assertEqual(self.store.listings()[0]["id"], new.id)
        with patch("backend.store.now", return_value="2026-11-05T11:00:00+00:00"):
            self.store.ingest("SS.lv", [old, fresh_ss])
            self.store.ingest("City24.lv", [fresh_city])
            self.store.update_image(old.id, "https://i.ss.lv/gallery/photo.800.jpg", 12345)
        self.assertEqual([item["id"] for item in self.store.listings()], ["latio:new", "city24:new", "ss:new", "ss:old"])

    def test_equal_time_uses_discovery_order_not_portal_name(self):
        """A later insertion wins ties regardless of alphabetical portal ID."""
        with patch("backend.store.now", return_value="2026-11-05T12:00:00+00:00"):
            self.store.ingest("scope", [listing("ss:zzz")])
            self.store.ingest("scope", [listing("city24:aaa")])
            self.store.ingest("scope", [listing("ss:zzz")])
        self.assertEqual([item["id"] for item in self.store.listings()], ["city24:aaa", "ss:zzz"])

    def test_publication_dates_sort_newest_first_across_timezones(self):
        """Use actual publication instants rather than source IDs or date-string order."""
        newest = listing("ss:newest")
        newest.published_at = "2026-11-05T10:05:00Z"
        middle = listing("city24:middle")
        middle.source = "City24.lv"
        middle.published_at = "2026-11-05T11:02:00+01:00"
        oldest = listing("latio:oldest")
        oldest.source = "Latio.lv"
        oldest.published_at = "2026-11-05T12:00:00+03:00"
        with patch("backend.store.now", return_value="2026-11-05T13:00:00+00:00"):
            self.store.ingest("scope", [newest, middle, oldest])
        self.assertEqual([item["id"] for item in self.store.listings()], [newest.id, middle.id, oldest.id])
        self.assertEqual(self.store.listings(limit=1)[0]["id"], newest.id)

    def test_missing_and_invalid_publication_dates_use_discovery(self):
        """Keep undated ads correctly placed using their original discovery timestamp."""
        undated = listing("ss:undated")
        invalid = listing("city24:invalid")
        invalid.published_at = "unknown"
        dated = listing("latio:dated")
        dated.published_at = "2026-11-05T11:00:00Z"
        for item, timestamp in [(undated, "10:00:00"), (invalid, "10:30:00"), (dated, "12:00:00")]:
            with patch("backend.store.now", return_value=f"2026-11-05T{timestamp}+00:00"):
                self.store.ingest("scope", [item])
        self.assertEqual([item["id"] for item in self.store.listings()], [dated.id, invalid.id, undated.id])

    def test_known_publication_date_survives_feed_rollover(self):
        """Do not lose a known date when a later public feed omits the same listing."""
        original = listing("ss:dated")
        original.published_at = "2026-11-04T10:00:00+02:00"
        self.store.ingest("scope", [original])
        self.store.ingest("scope", [listing(original.id)])
        self.assertEqual(self.store.listings()[0]["published_at"], original.published_at)

    def test_invalid_input(self):
        """Reject non-finite numbers, inverted limits and arbitrary source paths."""
        for data in [{"name": "x", "min": float("nan")}, {"name": "x", "min": 10 ** 1000}, {"name": "x", "min": 200, "max": 100}, {"name": "x", "locations": ["https://localhost/"]}]:
            with self.assertRaises(ValueError):
                self.store.save_search(data)


if __name__ == "__main__":
    unittest.main()