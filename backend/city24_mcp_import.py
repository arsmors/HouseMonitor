"""Fetch and import current City24 house-sale listings through MCP."""

import asyncio
import json
import re
import uuid
from urllib.parse import urlsplit

import aiohttp

from backend.sources import Listing, location_ids_for_text, normalized, now
from backend.store import Store

SCOPE = "City24.lv"
MCP_URL = "https://mcp.city24.lv/_mcp"
MCP_LOCATIONS = {
    "ss:riga-region/carnikavas-nov": "Carnikavas pagasts",
    "ss:riga/vecaki": "Vecāķi",
    "ss:riga/mangalsala": "Mangaļu pussala",
    "ss:riga/vecdaugava": "Vecdaugava",
    "ss:riga/trisciems": "Trīsciems",
    "ss:riga/jaunciems": "Jaunciems",
}
NON_RESIDENTIAL = re.compile(r"administrativ|biroj|komerc")
NON_RESIDENTIAL_IDS = {"8467022"}


async def _post_mcp(session, headers: dict, payload: dict) -> tuple[dict | None, str | None]:
    async with session.post(MCP_URL, headers=headers, json=payload) as response:
        chunks = []
        size = 0
        async for chunk in response.content.iter_chunked(65536):
            size += len(chunk)
            if size > 2_000_000:
                raise ValueError("City24 MCP response exceeds the safe size limit")
            chunks.append(chunk)
        if response.status >= 400:
            raise ValueError(f"City24 MCP returned HTTP {response.status}")
        session_id = response.headers.get("Mcp-Session-Id")
        body = b"".join(chunks)
        text = body.decode(response.charset or "utf-8", errors="replace")
        if "text/event-stream" in response.headers.get("Content-Type", ""):
            messages = []
            for block in text.split("\n\n"):
                data = "\n".join(line[6:] for line in block.splitlines() if line.startswith("data: "))
                if data and data != "[DONE]":
                    messages.append(json.loads(data))
            return (messages[-1] if messages else None), session_id
        return (json.loads(text) if text else None), session_id


def build_mcp_listings(results: dict[str, list[dict]], locations: dict[str, dict], existing: dict[str, dict] | None = None) -> list[Listing]:
    """Normalize exact-area MCP house results and retain locally enriched details."""
    existing = existing or {}
    result = {}
    for location_id, items in results.items():
        if location_id not in MCP_LOCATIONS or not isinstance(items, list):
            raise ValueError("City24 MCP returned an invalid location result")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("City24 MCP returned an invalid listing")
            if item.get("unitType") != "House" or item.get("transactionType") not in {"sell", "sale"}:
                continue
            identifier = item.get("id")
            title = item.get("title")
            price = item.get("price")
            if not isinstance(identifier, str) or not identifier.isdigit() or not isinstance(title, str) or not title.strip():
                raise ValueError("City24 MCP returned a listing without a valid ID or title")
            parsed_url = urlsplit(str(item.get("url", "")))
            if parsed_url.scheme != "https" or parsed_url.hostname != "www.city24.lv" or parsed_url.path != f"/real-estate/{identifier}":
                raise ValueError("City24 MCP returned an invalid ad URL")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0 or item.get("currency") != "EUR":
                raise ValueError("City24 MCP returned an invalid price")

            title = title.strip()
            cached = existing.get("city24:" + identifier, {})
            if identifier in NON_RESIDENTIAL_IDS or NON_RESIDENTIAL.search(normalized(title + " " + str(cached.get("title", "")))):
                continue
            listing_locations = location_ids_for_text(title + " " + str(cached.get("location", "")), locations)
            if location_id not in listing_locations:
                continue
            image = item.get("image")
            if image and (urlsplit(image).scheme != "https" or urlsplit(image).hostname != "static.img-city24.lv"):
                raise ValueError("City24 MCP returned an invalid image URL")
            if not image:
                image = cached.get("image")
            display_price = f"{price:,.2f}".rstrip("0").rstrip(".").replace(",", " ") + " €"
            area = cached.get("area")
            area_raw = cached.get("area_raw", "")
            result["city24:" + identifier] = Listing(
                "city24:" + identifier, "City24.lv", title, item["url"], display_price,
                cached.get("location") or title, listing_locations, area, area_raw,
                cached.get("area_status", "missing"), image, cached.get("published_at"),
                cached.get("address") or title.split(",", 1)[0].strip(),
            )
    return list(result.values())


async def fetch_mcp_results() -> dict[str, list[dict]]:
    """Query City24's supported MCP tool without requesting its website pages."""
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        initialized, session_id = await _post_mcp(session, headers, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "house-monitor", "version": "1.0"}},
        })
        if not initialized or "error" in initialized:
            raise ValueError("City24 MCP initialization failed")
        protocol = initialized.get("result", {}).get("protocolVersion")
        if not protocol:
            raise ValueError("City24 MCP did not provide a protocol version")
        headers["MCP-Protocol-Version"] = protocol
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        notification, _ = await _post_mcp(session, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        if notification and "error" in notification:
            raise ValueError("City24 MCP initialization notification failed")

        results = {}
        for index, (location_id, location_name) in enumerate(MCP_LOCATIONS.items()):
            if index:
                await asyncio.sleep(1)
            response, _ = await _post_mcp(session, headers, {
                "jsonrpc": "2.0", "id": index + 2, "method": "tools/call",
                "params": {"name": "search-listings", "arguments": {
                    "unitType": "House", "transactionType": "sale", "locationName": location_name,
                }},
            })
            if not response or "error" in response:
                raise ValueError(f"City24 MCP search failed for {location_name}")
            tool_result = response.get("result", {})
            if tool_result.get("isError"):
                raise ValueError(f"City24 MCP search failed for {location_name}")
            structured = tool_result.get("structuredContent")
            if not isinstance(structured, dict) or not isinstance(structured.get("listings"), list):
                raise ValueError(f"City24 MCP returned an invalid result for {location_name}")
            results[location_id] = structured["listings"]
        return results


def sync_mcp_listings(store: Store, listings: list[Listing]) -> dict:
    """Ingest the current MCP result set under one stable scope for incremental alerts."""
    result = store.ingest(SCOPE, listings)
    store.source_state("City24.lv", {
        "status": "ok", "last_success": now(), "failures": 0,
        "total": result["total"], "discovered": result["discovered"],
        "queued": result["queued"], "scope": SCOPE, "method": "City24 MCP",
    })
    store.event(
        f"City24 MCP: {result['total']} listings checked, "
        f"{result['discovered']} discovered, {result['queued']} notifications queued"
    )
    return result


async def refresh_mcp(store: Store) -> dict:
    """Fetch all configured areas and atomically sync the complete MCP result set."""
    existing = {
        listing["id"]: listing
        for listing in store.listings(10000)
        if listing.get("source") == "City24.lv"
    }
    results = await fetch_mcp_results()
    listings = build_mcp_listings(results, store.locations(), existing)
    return sync_mcp_listings(store, listings)


def main():
    """Refresh City24 from MCP in the shared database under a collector lease."""
    store = Store.from_environment("data/monitor.db", recover=False, require_remote=True, initialize=False)
    owner = "city24-mcp-import-" + uuid.uuid4().hex
    claimed = False
    try:
        claimed = store.acquire_lease(owner)
        if not claimed:
            raise RuntimeError("Another scheduled collector owns the database lease")
        result = asyncio.run(refresh_mcp(store))
        print(json.dumps(result, ensure_ascii=True))
    finally:
        if claimed:
            store.release_lease(owner)
        store.close()


if __name__ == "__main__":
    main()