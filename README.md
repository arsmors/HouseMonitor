# House Monitor

Live house-sale monitoring for Latvia, with a web dashboard and owner-only Telegram bot. Default areas: Carnikavas novads, Vecaki, Mangalsala, Vecdaugava, Trisciems, and Jaunciems (shown with Latvian spelling in the app).

## Local Setup

Python 3.12+ and Node.js 22+ are required. The local development checks used Python 3.14; Docker targets Python 3.12.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
npm ci
npm run build
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m backend.app
```

Open http://localhost:8787. For frontend development, run `npm run dev`. This starts the Python backend automatically, waits for its health check, and then starts Vite. An already healthy backend is reused and is not stopped when Vite exits. Otherwise, stopping the command stops both services; running it again starts both. The launcher uses the project's `.venv` Python when available and respects `HOST` and `PORT` from `.env` for the API proxy. Backend startup failures are reported in the terminal before the dashboard opens.

Set `TELEGRAM_BOT_TOKEN` from BotFather and your numeric `TELEGRAM_OWNER_ID` in `.env`, then restart the backend. Do not paste credentials into chat or commit `.env`. Send `/start` to your bot from that private owner account. Collection works without Telegram credentials; notifications wait in SQLite until the bot is connected and started.

Commands: `/start`, `/help`, `/add [name]`, `/searches`, `/edit ID`, `/pause ID`, `/resume ID`, `/delete ID`, `/latest ID`, `/status`, `/cancel`. The creation/edit flow accepts `/skip` for optional area limits and inline multi-location selection from SS.lv's actual hierarchy.

## Sources

- **SS.lv:** Public sale-category HTML pages, complete pagination, labelled house-area extraction, stable advertisement URLs, and publication metadata from public RSS when available. Buyer requests sometimes appear in the sale-category results; these are excluded. The general RSS feed mixes transaction types, so it is not used as proof that an advertisement is for sale.
- **City24.lv:** Ordinary public house-sale pages rendered with Chromium, complete visible pagination, no undocumented API. Searches are scoped to the selected locations, using verified public URLs for the four default areas and picker-generated URLs for other areas. The URLs are cached in SQLite; an all-Latvia crawl happens only for an explicitly unfiltered search. Separate area URLs prevent City24 from dropping mixed parish/neighbourhood filters. City24 calls Mangalsala "Mangalu pussala" and Carnikavas novads "Carnikavas pag."; both map to the canonical SS.lv IDs. If Chromium or the page is unavailable, the source reports an error and SS.lv continues. Disable with `CITY24_ENABLED=false` on very small hosts.
- **Latio.lv:** A public-card parser and pagination adapter are included, but collection is disabled by default. The site explicitly requires written permission to republish information. Set `LATIO_PERMISSION_CONFIRMED=true` only after obtaining appropriate permission. No claim is made that permission has been obtained.
- **RentInRiga.lv:** Public house-sale pages rendered with Chromium, using separately verified geographic filters for the selected areas and complete pagination within each filter. Only matching locations are retained, and house area is read from the labelled `Platība` field rather than plot size or monthly finance estimates. The page's initial empty state is not accepted before hydration completes. Disable with `RENTINRIGA_ENABLED=false` if needed. The first complete scan establishes a silent baseline like the other sources.

Additional sites were evaluated but not enabled: Varianti.lv and ARCO explicitly restrict reuse of their content without authorization, while Ober-Haus's public search did not return reliably loaded results during verification. No unsupported or fabricated listings from these sites are shown.

Robots rules are checked, source hosts are allowlisted, requests are bounded and spaced, and access blocks are reported instead of bypassed. Review provider terms for your intended personal use, particularly any public redistribution of descriptions or photographs. There is no cross-site property-identity matching: the same house advertised on different sites is a separate advertisement.

## Monitoring And Delivery

Recent listings shows saved sale ads in Vecdaugava, Vecaki, Carnikavas novads, Trisciems, and Jaunciems, with optional location filters; browsing does not require saved searches. History from other areas is retained but not displayed. The web dashboard no longer has a My searches tab. With no saved searches, collection continues in these five areas without queuing search-based Telegram alerts. Existing Telegram/API search configuration remains supported.

Scans start every hour (3,600 seconds) when possible and never overlap. Override this with `POLL_INTERVAL` in `.env`. A slow or unavailable source can increase latency. New source/category coverage gets a silent complete baseline. IDs, search history, and a unique `(search_id, listing_id)` outbox persist in SQLite. Editing or deleting a search does not remove listing history.

Each source has a four-minute check deadline, with a bounded browser shutdown grace period. A timeout keeps the last complete listings and records an error instead of leaving the dashboard in a checking state. The next scheduled check is persisted across restarts; restarting does not continuously recheck sources. Overlong cycles wait a full configured interval rather than immediately scanning again. Manual checks remain rate-limited.

During scans, the dashboard checks progress every 15 seconds without rebuilding unchanged listing cards. Open price histories stay expanded when refreshed listing data arrives.

SS.lv listings shown in Recent listings, as well as listings matching active searches, get their larger gallery photo from the public advertisement page in a separate background worker. Photo upgrades do not require a saved search. Photo URLs are cached in SQLite for a day, survive subsequent scans, and refresh when the source thumbnail changes. Detail-page failures retain the available thumbnail without disrupting collection. Photos and listing titles open the original advertisement in a new tab; the card's Telegram icon opens the notification preview.

Listings are ordered by publication date, newest first across all portals. When no valid publication date is available, the original discovery time is used and labelled "Discovered" on the card. Dates are compared as actual instants across timezones. Refreshing old advertisements or photos does not change their discovery time, and known publication dates survive RSS feed rollover.

Price history records observed price changes and shows them on listing cards. Relists are grouped only when the source, exact normalized street address and location, and house area match; ads without those details retain price history only under their original ad ID. Buyer-request ads already stored in SQLite are removed with their related search and notification records at startup.

Detected relists show a "Relisted" badge above the price and are ordered by the new advertisement's discovery time rather than an older publication date. Subsequent refreshes do not bump them again; newer ads can appear above them.

Confirmed rate-limit rejections and rejected deliveries remain retryable. If Telegram's response is lost, the bot cannot know whether a message arrived. Such jobs are marked `uncertain` and are not resent automatically. After checking the chat, an operator can mark a job `sent` or set `status='pending', next_attempt=0` in the SQLite `notifications` table. This avoids claiming an impossible exactly-once guarantee. Interrupted `sending` jobs are also quarantined after restart.

Pausing a search stops new notifications and delivery of its queued jobs until resumed. A repeated source failure triggers one owner warning after three failures, with increasing backoff. The dashboard's Activity page shows source errors and queue states. Initial baseline listings are visible on the dashboard but are not pushed to Telegram.

## VPS Deployment

Create `.env`, set Telegram credentials and a strong `WEB_PASSWORD`, then:

```sh
docker compose up -d --build
docker compose logs -f --tail=100
```

The dashboard port is bound only to the VPS loopback interface. Use an SSH tunnel (`ssh -L 8787:127.0.0.1:8787 user@your-vps`), open http://localhost:8787, and sign in as `owner` with `WEB_PASSWORD`. Do not expose basic authentication over public unencrypted HTTP. A public deployment needs an HTTPS reverse proxy and appropriate access controls. Telegram uses outbound HTTPS long polling and requires no inbound bot port.

SQLite lives in the `monitor-data` named volume. Do not remove it during upgrades. Back up with SQLite's backup API or stop the container before copying the database and WAL files. A Docker restart policy and process healthcheck are included. A healthy process does not imply every external source is available.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test scripts/dev.test.mjs
npm run build
```

Tests cover source-area extraction, geography, inclusive filters, missing area, baselines, duplicate detection, persistent retry state, pause/resume, edit history, restart recovery, owner authorization, delivery outcomes, and API validation/security. Real Telegram delivery requires your credentials. Docker deployment and a 24-hour soak test must be run on the target VPS before production sign-off.

With the local service running and Telegram left unconfigured, `.\.venv\Scripts\python.exe tests/browser_smoke.py` exercises real dashboard search actions and writes desktop/mobile screenshots to `data/screenshots`. It creates and then deletes its own temporary search. Source snapshots change as new advertisements are published.

`.\.venv\Scripts\python.exe tests/browser_smoke.py --refresh-only` tests the manual-check progress timer with a controlled browser clock and no source scans or database writes. Manual checks poll UI status every 15 seconds until complete; idle dashboard refresh stays aligned with the configured monitoring interval (one hour by default).