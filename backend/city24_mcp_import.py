"""Import the captured City24 MCP snapshot into the shared database once."""

import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from backend.sources import Listing, RECENT_LOCATION_IDS, now
from backend.store import Store

SNAPSHOT_PATH = Path(__file__).with_name("city24_mcp_snapshot.json")
SCOPE = "city24:mcp-snapshot:2026-11-10"


def load_snapshot(path: Path = SNAPSHOT_PATH) -> list[Listing]:
    """Validate captured MCP house-sale records before they reach persistence."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("City24 MCP snapshot must contain a non-empty listing array")

    listings = []
    identifiers = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("City24 MCP snapshot entry is invalid")
        identifier = item.get("id")
        location_id = item.get("location_id")
        price = item.get("price")
        area = item.get("area")
        url = urlsplit(str(item.get("url", "")))
        image = item.get("image")
        if not isinstance(identifier, str) or not identifier.isdigit() or identifier in identifiers:
            raise ValueError("City24 MCP snapshot has a missing or duplicate ad ID")
        if item.get("unitType") != "House" or item.get("transactionType") != "sell":
            raise ValueError("City24 MCP snapshot contains a non-sale house")
        if location_id not in RECENT_LOCATION_IDS:
            raise ValueError("City24 MCP snapshot contains an unsupported location")
        if url.scheme != "https" or url.hostname != "www.city24.lv" or url.path != f"/real-estate/{identifier}":
            raise ValueError("City24 MCP snapshot contains an invalid ad URL")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0 or item.get("currency") != "EUR":
            raise ValueError("City24 MCP snapshot contains an invalid price")
        if area is not None and (isinstance(area, bool) or not isinstance(area, (int, float)) or area <= 0):
            raise ValueError("City24 MCP snapshot contains an invalid house area")
        if image and (urlsplit(image).scheme != "https" or urlsplit(image).hostname != "static.img-city24.lv"):
            raise ValueError("City24 MCP snapshot contains an invalid image URL")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("City24 MCP snapshot contains an empty title")

        parent = location_id.rsplit("/", 1)[0]
        listings.append(Listing(
            "city24:" + identifier, "City24.lv", title.strip(), item["url"],
            f'{price:,.0f} €'.replace(",", " "), title.strip(), [parent, location_id],
            area, f"{area:g}" if area is not None else "", "parsed" if area is not None else "missing",
            image, address=title.split(",", 1)[0].strip(),
        ))
        identifiers.add(identifier)
    return listings


def import_snapshot(store: Store, listings: list[Listing]) -> dict:
    """Add only missing IDs in one silent baseline without overwriting existing ads."""
    if any(listing.source != "City24.lv" for listing in listings):
        raise ValueError("Only City24 listings may be imported")
    existing = {row["id"] for row in store.db.execute("SELECT id FROM listings")}
    missing = [listing for listing in listings if listing.id not in existing]
    already_present = len(listings) - len(missing)
    result = {"baseline": None, "queued": 0, "discovered": 0}
    if missing:
        if store.db.execute("SELECT 1 FROM scopes WHERE id=?", (SCOPE,)).fetchone():
            raise ValueError("City24 MCP import scope already exists but snapshot IDs are missing; refusing a partial import")
        result = store.ingest(SCOPE, missing)
        if result["queued"]:
            raise RuntimeError("City24 MCP snapshot unexpectedly queued notifications")

    added = result["discovered"]
    store.source_state("City24.lv", {
        "status": "ok", "last_success": now(), "failures": 0,
        "total": len(listings), "discovered": added, "queued": 0,
        "scope": SCOPE, "method": "City24 MCP snapshot import",
    })
    store.event(f"City24 MCP snapshot imported: {added} added, {already_present} already present")
    return {"total": len(listings), "added": added, "already_present": already_present, "queued": 0}


def main():
    """Require the hosted database and a collector lease for the import."""
    store = Store.from_environment("data/monitor.db", recover=False, require_remote=True, initialize=False)
    owner = "city24-mcp-import-" + uuid.uuid4().hex
    claimed = False
    try:
        claimed = store.acquire_lease(owner)
        if not claimed:
            raise RuntimeError("Another scheduled collector owns the database lease")
        result = import_snapshot(store, load_snapshot())
        print(json.dumps(result, ensure_ascii=True))
    finally:
        if claimed:
            store.release_lease(owner)
        store.close()


if __name__ == "__main__":
    main()