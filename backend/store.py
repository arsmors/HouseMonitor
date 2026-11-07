"""SQLite persistence, atomic baselines and notification outbox."""

import json
import math
import sqlite3
from pathlib import Path

from backend.sources import DEFAULT_LABELS, DEFAULT_PATHS, Listing, is_sale_listing, match, now, relist_key


class Store:
    """Single-process database owner with short, atomic transactions."""

    def __init__(self, path: str):
        """Create the database and seed real default category IDs once."""
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                min REAL, max REAL, locations TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS listings (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, data TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS listing_price_history (
                id INTEGER PRIMARY KEY, history_key TEXT NOT NULL,
                listing_id TEXT NOT NULL REFERENCES listings(id),
                price TEXT NOT NULL, observed_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS listing_price_history_key ON listing_price_history(history_key,id);
            CREATE TABLE IF NOT EXISTS scopes (id TEXT PRIMARY KEY, baseline_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS search_history (
                search_id INTEGER NOT NULL REFERENCES searches(id),
                listing_id TEXT NOT NULL REFERENCES listings(id),
                discovered_at TEXT NOT NULL, PRIMARY KEY(search_id, listing_id));
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY, search_id INTEGER NOT NULL REFERENCES searches(id),
                listing_id TEXT NOT NULL REFERENCES listings(id), status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                sent_at TEXT, message_id INTEGER, error TEXT,
                UNIQUE(search_id, listing_id));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS locations (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS source_states (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, message TEXT NOT NULL);
        """)
        with self.db:
            self.db.execute("UPDATE notifications SET status='uncertain', error='Process stopped during delivery; inspect Telegram before retrying' WHERE status='sending'")
            if self.setting("initialized") is None:
                self.db.execute("INSERT INTO searches(name,min,max,locations,created_at) VALUES(?,NULL,NULL,?,?)", ("Our next home", json.dumps(["ss:" + path for path in DEFAULT_PATHS]), now()))
                self.set_setting("initialized", True)
            for path, label in zip(DEFAULT_PATHS, DEFAULT_LABELS):
                record = {"id": "ss:" + path, "label": label, "path": path, "parent": "ss:" + path.rsplit("/", 1)[0], "group": "Selected areas"}
                self.db.execute("INSERT OR IGNORE INTO locations VALUES(?,?)", (record["id"], json.dumps(record)))
        self.removed_buyer_requests = self.remove_buyer_requests()
        self._backfill_price_history()

    def close(self):
        """Release the SQLite handle on service shutdown."""
        self.db.close()

    def setting(self, key: str, default=None):
        """Read a JSON setting without exposing environment secrets."""
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value):
        """Persist one service setting."""
        with self.db:
            self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def locations(self) -> dict[str, dict]:
        """Return the cached, source-discovered geographic catalog."""
        return {row["id"]: json.loads(row["data"]) for row in self.db.execute("SELECT * FROM locations")}

    def save_locations(self, records: list[dict]):
        """Cache verified source category nodes."""
        with self.db:
            self.db.executemany("INSERT INTO locations VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", [(record["id"], json.dumps(record)) for record in records])

    def searches(self) -> list[dict]:
        """Return search configuration with preserved history counts."""
        result = []
        for row in self.db.execute("SELECT s.*, (SELECT count(*) FROM search_history h WHERE h.search_id=s.id) AS count FROM searches s WHERE deleted=0 ORDER BY id"):
            record = dict(row)
            record["locations"] = json.loads(record["locations"])
            record["active"] = bool(record["active"])
            result.append(record)
        return result

    def save_search(self, data: dict, search_id: int | None = None) -> dict:
        """Validate search input before a parameterized insert or update."""
        name = data.get("name", "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("Search name must contain 1 to 60 characters")
        for key in ["min", "max"]:
            value = data.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 < value <= 1_000_000 or not math.isfinite(value)):
                raise ValueError("Area must be a positive number or empty")
        if data.get("min") is not None and data.get("max") is not None and data["min"] > data["max"]:
            raise ValueError("Maximum area must be at least the minimum area")
        locations = data.get("locations", [])
        if not isinstance(locations, list) or len(locations) > 100 or any(not isinstance(location, str) or location not in self.locations() for location in locations):
            raise ValueError("Select valid locations from the SS.lv hierarchy")
        if "active" in data and not isinstance(data["active"], bool):
            raise ValueError("Active must be a boolean")
        existing = next((search for search in self.searches() if search["id"] == search_id), None)
        if search_id is not None and existing is None:
            raise ValueError("Search not found")
        values = (name.strip(), data.get("min"), data.get("max"), json.dumps(list(dict.fromkeys(locations))), int(data.get("active", existing["active"] if existing else True)))
        with self.db:
            if existing:
                self.db.execute("UPDATE searches SET name=?,min=?,max=?,locations=?,active=? WHERE id=?", (*values, search_id))
            else:
                cursor = self.db.execute("INSERT INTO searches(name,min,max,locations,active,created_at) VALUES(?,?,?,?,?,?)", (*values, now()))
                search_id = cursor.lastrowid
        self.event("Search updated" if existing else "Search created")
        return next(search for search in self.searches() if search["id"] == search_id)

    def delete_search(self, search_id: int):
        """Soft-delete a search and cancel its unsent deliveries, retaining history."""
        with self.db:
            self.db.execute("UPDATE searches SET deleted=1,active=0 WHERE id=?", (search_id,))
            self.db.execute("UPDATE notifications SET status='cancelled' WHERE search_id=? AND status IN ('pending','failed')", (search_id,))

    def remove_buyer_requests(self) -> int:
        """Permanently remove legacy SS.lv buyer requests and their related history."""
        rows = self.db.execute("SELECT id,data FROM listings WHERE source='SS.lv'").fetchall()
        identifiers = [row["id"] for row in rows if not is_sale_listing(json.loads(row["data"]))]
        if not identifiers:
            return 0
        placeholders = ",".join("?" for _ in identifiers)
        with self.db:
            self.db.execute(f"DELETE FROM notifications WHERE listing_id IN ({placeholders})", identifiers)
            self.db.execute(f"DELETE FROM search_history WHERE listing_id IN ({placeholders})", identifiers)
            self.db.execute(f"DELETE FROM listing_price_history WHERE listing_id IN ({placeholders})", identifiers)
            self.db.execute(f"DELETE FROM listings WHERE id IN ({placeholders})", identifiers)
        return len(identifiers)

    def _backfill_price_history(self):
        """Seed known current prices as the starting point for exact-address histories."""
        with self.db:
            for row in self.db.execute("SELECT id,data,first_seen_at FROM listings ORDER BY julianday(first_seen_at),rowid").fetchall():
                record = json.loads(row["data"])
                key = relist_key(record)
                price = str(record.get("price", "")).strip()
                if key and price:
                    self.db.execute(
                        "INSERT INTO listing_price_history(history_key,listing_id,price,observed_at) "
                        "SELECT ?,?,?,? WHERE NOT EXISTS "
                        "(SELECT 1 FROM listing_price_history WHERE history_key=? AND listing_id=?)",
                        (key, row["id"], price, row["first_seen_at"], key, row["id"]),
                    )

    def ingest(self, scope: str, listings: list[Listing]) -> dict:
        """Atomically store a complete scope, its baseline, matches and outbox jobs."""
        listings = [listing for listing in listings if is_sale_listing(listing.record())]
        active = [search for search in self.searches() if search["active"]]
        baseline = self.db.execute("SELECT 1 FROM scopes WHERE id=?", (scope,)).fetchone() is None
        timestamp = now()
        discovered = 0
        queued = 0
        with self.db:
            for listing in listings:
                record = listing.record()
                previous = self.db.execute("SELECT data,first_seen_at FROM listings WHERE id=?", (listing.id,)).fetchone()
                existing = previous is not None
                cached = None
                if previous:
                    cached = json.loads(previous["data"])
                    if not record.get("published_at") and cached.get("published_at"):
                        record["published_at"] = cached["published_at"]
                    if listing.source == "SS.lv" and cached.get("image_checked_at") and cached.get("image_thumbnail") == record.get("image"):
                        for key in ["image", "image_thumbnail", "image_checked_at"]:
                            record[key] = cached[key]
                self.db.execute("INSERT INTO listings VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,last_seen_at=excluded.last_seen_at", (listing.id, listing.source, json.dumps(record), timestamp, timestamp))
                key = relist_key(record)
                price = str(record.get("price", "")).strip()
                if key and price:
                    latest = self.db.execute("SELECT listing_id,price FROM listing_price_history WHERE history_key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
                    if latest is None and cached and cached.get("price"):
                        self.db.execute("INSERT INTO listing_price_history(history_key,listing_id,price,observed_at) VALUES(?,?,?,?)", (key, listing.id, str(cached["price"]).strip(), previous["first_seen_at"]))
                        latest = {"listing_id": listing.id, "price": str(cached["price"]).strip()}
                    if latest is None or latest["listing_id"] != listing.id or latest["price"] != price:
                        self.db.execute("INSERT INTO listing_price_history(history_key,listing_id,price,observed_at) VALUES(?,?,?,?)", (key, listing.id, price, timestamp))
                discovered += not existing
                for search in active:
                    if not match(record, search):
                        continue
                    self.db.execute("INSERT OR IGNORE INTO search_history VALUES(?,?,?)", (search["id"], listing.id, timestamp))
                    if not baseline and not existing:
                        queued += self.db.execute("INSERT OR IGNORE INTO notifications(search_id,listing_id) VALUES(?,?)", (search["id"], listing.id)).rowcount
            self.db.execute("INSERT OR IGNORE INTO scopes VALUES(?,?)", (scope, timestamp))
        return {"baseline": baseline, "discovered": discovered, "queued": queued, "total": len(listings)}

    def listings(self, limit: int = 1000) -> list[dict]:
        """Sort relists by rediscovery and other ads by publication or discovery."""
        rows = self.db.execute(
            "SELECT *,rowid AS discovery_order,julianday(first_seen_at) AS discovered,"
            "COALESCE(julianday(json_extract(data, '$.published_at')),julianday(first_seen_at)) AS published "
            "FROM listings"
        ).fetchall()
        ordering = {row["id"]: (row["published"], row["discovered"], row["discovery_order"]) for row in rows}
        records = [{**json.loads(row["data"]), "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"]} for row in rows]
        records = [record for record in records if is_sale_listing(record)]
        history_keys = {relist_key(record) for record in records}
        history_keys.discard(None)
        histories = {}
        if history_keys:
            placeholders = ",".join("?" for _ in history_keys)
            for row in self.db.execute(
                f"SELECT history_key,listing_id,price,observed_at FROM listing_price_history "
                f"WHERE history_key IN ({placeholders}) ORDER BY julianday(observed_at),id",
                tuple(history_keys),
            ):
                histories.setdefault(row["history_key"], []).append(
                    {"listing_id": row["listing_id"], "price": row["price"], "observed_at": row["observed_at"]}
                )
        for record in records:
            record["price_history"] = histories.get(relist_key(record), [])
            current = ordering[record["id"]]
            record["relisted_at"] = None
            for observation in record["price_history"]:
                previous = ordering[observation["listing_id"]]
                if observation["listing_id"] != record["id"] and previous[1:] < current[1:]:
                    record["relisted_at"] = record["first_seen_at"]
                    break
        records.sort(
            key=lambda record: (
                ordering[record["id"]][1] if record["relisted_at"] else ordering[record["id"]][0],
                *ordering[record["id"]][1:],
            ),
            reverse=True,
        )
        return records[:limit] if limit >= 0 else records

    def update_image(self, listing_id: str, image: str | None, checked_at: float):
        """Persist photo enrichment without changing discovery times or notification history."""
        row = self.db.execute("SELECT data FROM listings WHERE id=?", (listing_id,)).fetchone()
        if not row:
            return
        record = json.loads(row["data"])
        record.setdefault("image_thumbnail", record.get("image"))
        record["image_checked_at"] = checked_at
        if image:
            record["image"] = image
        with self.db:
            self.db.execute("UPDATE listings SET data=? WHERE id=?", (json.dumps(record), listing_id))

    def pending(self, timestamp: float) -> list[dict]:
        """Get due jobs only for active searches while delivery is enabled."""
        if not self.setting("notifications_enabled", True):
            return []
        rows = self.db.execute("SELECT n.* FROM notifications n JOIN searches s ON s.id=n.search_id WHERE n.status IN ('pending','failed') AND n.next_attempt<=? AND s.active=1 AND s.deleted=0 ORDER BY n.id LIMIT 30", (timestamp,)).fetchall()
        return [dict(row) for row in rows]

    def delivery(self, notification_id: int, status: str, error: str | None = None, next_attempt: float = 0, message_id: int | None = None):
        """Record delivery outcome; uncertain deliveries are never retried automatically."""
        with self.db:
            self.db.execute("UPDATE notifications SET status=?,error=?,next_attempt=?,message_id=?,sent_at=?,attempts=attempts+? WHERE id=?", (status, error, next_attempt, message_id, now() if status == "sent" else None, int(status == "sending"), notification_id))

    def source_state(self, source: str, state: dict):
        """Persist source health across process restarts."""
        with self.db:
            self.db.execute("INSERT INTO source_states VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (source, json.dumps(state)))

    def event(self, message: str):
        """Keep a bounded human-readable activity log."""
        with self.db:
            self.db.execute("INSERT INTO events(created_at,message) VALUES(?,?)", (now(), message[:500]))
            self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 100)")

    def status(self) -> dict:
        """Expose non-secret monitoring status and queue counters."""
        return {"sources": {row["id"]: json.loads(row["data"]) for row in self.db.execute("SELECT * FROM source_states")},
                "events": [dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 30")],
                "queue": {row["status"]: row["total"] for row in self.db.execute("SELECT status,count(*) AS total FROM notifications GROUP BY status")},
                "notifications_enabled": self.setting("notifications_enabled", True)}