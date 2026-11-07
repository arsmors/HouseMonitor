"""Persistence, baseline, queue and search lifecycle acceptance tests."""

import tempfile
import unittest
import json
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

    def test_default_search_includes_all_six_areas(self):
        """Seed the default search and catalog with every configured area."""
        expected = {
            "ss:riga-region/carnikavas-nov",
            "ss:riga/vecaki",
            "ss:riga/mangalsala",
            "ss:riga/vecdaugava",
            "ss:riga/trisciems",
            "ss:riga/jaunciems",
        }
        self.assertEqual(set(self.store.searches()[0]["locations"]), expected)
        self.assertTrue(expected.issubset(self.store.locations()))

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

    def test_ingest_excludes_buyer_requests(self):
        """Do not persist buyer requests as sale listings."""
        buyer_request = listing("ss:wanted")
        buyer_request.title = "Nopirksim māju"
        buyer_request.price = "pērku"
        result = self.store.ingest("scope", [buyer_request, listing("ss:for-sale")])
        self.assertEqual(result["total"], 1)
        self.assertEqual([item["id"] for item in self.store.listings()], ["ss:for-sale"])

    def test_cleanup_removes_legacy_buyer_request_history(self):
        """Delete old buyer-request rows and their search and notification references."""
        buyer_request = listing("ss:legacy-wanted")
        buyer_request.title = "Nopirksim māju"
        buyer_request.price = "pērku"
        search_id = self.store.searches()[0]["id"]
        with self.store.db:
            self.store.db.execute("INSERT INTO listings VALUES(?,?,?,?,?)", (buyer_request.id, buyer_request.source, json.dumps(buyer_request.record()), "2026-11-05T10:00:00+00:00", "2026-11-05T10:00:00+00:00"))
            self.store.db.execute("INSERT INTO search_history VALUES(?,?,?)", (search_id, buyer_request.id, "2026-11-05T10:00:00+00:00"))
            self.store.db.execute("INSERT INTO notifications(search_id,listing_id) VALUES(?,?)", (search_id, buyer_request.id))
        self.assertEqual(self.store.remove_buyer_requests(), 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM listings WHERE id=?", (buyer_request.id,)).fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM search_history WHERE listing_id=?", (buyer_request.id,)).fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM notifications WHERE listing_id=?", (buyer_request.id,)).fetchone()[0], 0)

    def test_price_history_tracks_changes_and_exact_address_relists(self):
        """Record price changes and join new IDs only for exact address and area."""
        original = listing("ss:old")
        original.address = "  14 Šalkones   iela "
        with patch("backend.store.now", return_value="2026-11-05T10:00:00+00:00"):
            self.store.ingest("scope", [original])
        repriced = listing("ss:old")
        repriced.address = "14 Šalkones iela"
        repriced.price = "95,000 €"
        with patch("backend.store.now", return_value="2026-11-06T10:00:00+00:00"):
            self.store.ingest("scope", [repriced])
        relisted = listing("ss:new")
        relisted.address = "14 Šalkones iela"
        relisted.price = "90,000 €"
        other_house = listing("ss:other")
        other_house.address = "16 Šalkones iela"
        other_house.price = "85,000 €"
        same_street_elsewhere = listing("ss:elsewhere")
        same_street_elsewhere.address = relisted.address
        same_street_elsewhere.location = "Carnikava"
        same_street_elsewhere.price = "80,000 €"
        with patch("backend.store.now", return_value="2026-11-07T10:00:00+00:00"):
            self.store.ingest("scope", [relisted, other_house, same_street_elsewhere])
        records = {record["id"]: record for record in self.store.listings()}
        history = records["ss:new"]["price_history"]
        self.assertEqual([entry["price"] for entry in history], ["100,000 €", "95,000 €", "90,000 €"])
        self.assertEqual([entry["listing_id"] for entry in history], ["ss:old", "ss:old", "ss:new"])
        self.assertEqual([entry["price"] for entry in records["ss:other"]["price_history"]], ["85,000 €"])
        self.assertEqual([entry["price"] for entry in records["ss:elsewhere"]["price_history"]], ["80,000 €"])

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

    def test_relist_moves_to_top_without_refreshing_its_discovery_time(self):
        """A new ID for a known house outranks newer publication dates on older ads."""
        original = listing("ss:original")
        original.address = "Street 14"
        original.published_at = "2026-11-01T10:00:00+00:00"
        other = listing("ss:other-house")
        other.address = "Street 16"
        other.published_at = "2026-11-06T10:00:00+00:00"
        with patch("backend.store.now", return_value="2026-11-06T12:00:00+00:00"):
            self.store.ingest("scope", [original, other])
        relisted = listing("ss:relisted")
        relisted.address = original.address
        relisted.published_at = original.published_at
        with patch("backend.store.now", return_value="2026-11-07T10:00:00+00:00"):
            self.store.ingest("scope", [relisted])
        top = self.store.listings(limit=1)[0]
        self.assertEqual(top["id"], relisted.id)
        self.assertEqual(top["relisted_at"], "2026-11-07T10:00:00+00:00")
        records = {record["id"]: record for record in self.store.listings()}
        self.assertIsNone(records[original.id]["relisted_at"])
        self.assertIsNone(records[other.id]["relisted_at"])
        with patch("backend.store.now", return_value="2026-11-08T10:00:00+00:00"):
            self.store.ingest("scope", [original, relisted])
        self.assertEqual(self.store.listings(limit=1)[0]["relisted_at"], top["relisted_at"])

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