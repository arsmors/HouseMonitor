"""Public-page adapters. Never interpret land area as house area."""

import asyncio
import base64
import math
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp
import feedparser
from bs4 import BeautifulSoup

SS_ROOT = "https://www.ss.lv/lv/real-estate/homes-summer-residences/"
DEFAULT_PATHS = ["riga-region/carnikavas-nov", "riga/vecaki", "riga/mangalsala", "riga/vecdaugava"]
DEFAULT_LABELS = ["Carnikavas novads", "Vecāķi", "Mangaļsala", "Vecdaugava"]
CITY24_AREA_SEARCHES = {
    "ss:riga-region/carnikavas-nov": "https://www.city24.lv/real-estate-search/houses-for-sale/adazu-nov-carnikavas-pag/id=245490-parish",
    "ss:riga/mangalsala": "https://www.city24.lv/real-estate-search/houses-for-sale/riga-mangalu-pussala/id=270744-district",
    "ss:riga/vecaki": "https://www.city24.lv/real-estate-search/houses-for-sale/riga-vecaki/id=270711-district",
    "ss:riga/vecdaugava": "https://www.city24.lv/real-estate-search/houses-for-sale/riga-vecdaugava/id=270696-district",
}
USER_AGENT = "HouseMonitor/1.0 (personal property notifications)"


def now() -> str:
    """Return a sortable UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def normalized(value: str) -> str:
    """Normalize spelling for exact geographic component comparisons."""
    return "".join(character for character in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(character)).strip()


def parse_area(raw: str | None) -> tuple[float | None, str]:
    """Parse an explicitly labelled house-area value, rejecting ranges and land units."""
    if not raw or raw.strip() in {"-", "—", ""}:
        return None, "missing"
    value = re.sub(r"\s|\u00a0|\u202f", "", raw.lower())
    value = re.sub(r"(?:m²|m2|kv\.?m\.?)$", "", value)
    if not re.fullmatch(r"\d+(?:[.,]\d+)?", value):
        return None, "unparseable"
    number = float(value.replace(",", "."))
    return (number, "parsed") if math.isfinite(number) and number > 0 else (None, "unparseable")


@dataclass
class Listing:
    """Normalized source listing with raw extraction evidence."""

    id: str
    source: str
    title: str
    url: str
    price: str
    location: str
    location_ids: list[str]
    area: float | None
    area_raw: str
    area_status: str
    image: str | None = None
    published_at: str | None = None

    def record(self) -> dict:
        """Serialize the listing without losing parser evidence."""
        return asdict(self)


def parse_ss(html: str, page_url: str, locations: dict[str, dict]) -> list[Listing]:
    """Read house area by table heading, not position in advertisement text."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("tr#head_line")
    if heading is None:
        raise ValueError("SS.lv listing table missing; source may have changed or blocked access")
    headers = [cell.get_text(" ", strip=True) for cell in heading.find_all("td", recursive=False) for _ in range(int(cell.get("colspan", 1)))]
    area_index = next((index for index, value in enumerate(headers) if value in {"m²", "m2"}), None)
    if area_index is None:
        raise ValueError("SS.lv house-area column missing")
    result = []
    for row in soup.select('tr[id^="tr_"]'):
        anchor = row.select_one("a.am[href]")
        if anchor is None:
            continue
        cells = row.find_all("td", recursive=False)
        if len(cells) != len(headers):
            raise ValueError("SS.lv listing row does not match the column headings")
        url = urljoin(page_url, anchor["href"])
        if urlsplit(url).hostname not in {"www.ss.lv", "www.ss.com"}:
            continue
        path = urlsplit(url).path.split("homes-summer-residences/", 1)[-1].rsplit("/", 1)[0]
        if "houses-abroad-latvia" in path:
            continue
        location_ids = ["ss:" + "/".join(path.split("/")[:depth]) for depth in range(1, len(path.split("/")) + 1)]
        labels = [locations[location_id]["label"] for location_id in location_ids if location_id in locations]
        raw = cells[area_index].get_text(" ", strip=True)
        area, status = parse_area(raw)
        image = row.select_one("img.isfoto")
        identifier = urlsplit(url).path.rsplit("/", 1)[-1].removesuffix(".html")
        result.append(Listing("ss:" + identifier, "SS.lv", anchor.get_text(" ", strip=True), url,
                              cells[-1].get_text(" ", strip=True), ", ".join(reversed(labels)) or path,
                              location_ids, area, raw, status, urljoin(url, image["src"]) if image else None))
    return result


def parse_ss_image(html: str, page_url: str) -> str | None:
    """Use a published gallery photo, never a guessed thumbnail filename."""
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select('a[onclick^="big_pic("][href]'):
        url = urljoin(page_url, anchor["href"])
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname in {"i.ss.lv", "i.ss.com"} and parsed.path.startswith("/gallery/") and re.search(r"\.(?:jpe?g|png|webp)$", parsed.path, re.IGNORECASE) and not re.search(r"\.(?:th\d*|t)\.", parsed.path):
            return url
    return None


def match(listing: dict, search: dict) -> bool:
    """Apply inclusive house-area limits and exact geographic IDs."""
    if search["locations"] and not set(search["locations"]).intersection(listing["location_ids"]):
        return False
    area = listing["area"]
    if search.get("min") is not None and (area is None or area < search["min"]):
        return False
    if search.get("max") is not None and (area is None or area > search["max"]):
        return False
    return True


class PublicHTTP:
    """Bounded, robot-aware HTTP with conditional caching and respectful retries."""

    def __init__(self, session: aiohttp.ClientSession):
        """Share one client and serialize source requests."""
        self.session = session
        self.lock = asyncio.Lock()
        self.robots: dict[str, RobotFileParser] = {}
        self.cache: dict[str, tuple[str, str, str]] = {}
        self.next_request = 0.0

    async def get(self, url: str) -> str:
        """Fetch only known source hosts and stop on forbidden/challenge responses."""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"www.ss.lv", "www.ss.com", "www.city24.lv", "latio.lv"}:
            raise ValueError("Source URL is not allowed")
        origin = f"https://{parsed.hostname}"
        if origin not in self.robots:
            async with self.session.get(origin + "/robots.txt", allow_redirects=False) as response:
                if response.status >= 400:
                    raise ValueError(f"Cannot verify {parsed.hostname} robots.txt ({response.status})")
                policy = RobotFileParser()
                policy.parse((await response.text()).splitlines())
                self.robots[origin] = policy
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            raise ValueError("Source robots.txt does not permit this URL")
        async with self.lock:
            for attempt in range(3):
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + 1.0
                cached = self.cache.get(url)
                headers = {}
                if cached:
                    if cached[1]:
                        headers["If-None-Match"] = cached[1]
                    if cached[2]:
                        headers["If-Modified-Since"] = cached[2]
                async with self.session.get(url, headers=headers, allow_redirects=False) as response:
                    if response.status == 304 and cached:
                        return cached[0]
                    if response.status == 429 or response.status >= 500:
                        retry = response.headers.get("Retry-After", "")
                        try:
                            delay = float(retry) if retry.isdigit() else max(0, parsedate_to_datetime(retry).timestamp() - time.time())
                        except (ValueError, TypeError):
                            delay = 2 ** (attempt + 1)
                        self.next_request = time.monotonic() + max(delay, 1)
                        if delay > 60 or attempt == 2:
                            raise ValueError(f"Source temporarily unavailable ({response.status}); retry after {delay:.0f}s")
                        continue
                    if response.status != 200:
                        raise ValueError(f"Source returned HTTP {response.status}; no bypass attempted")
                    content = await response.content.read(5_000_001)
                    if len(content) > 5_000_000:
                        raise ValueError("Source response exceeds the safe size limit")
                    text = content.decode(response.charset or "utf-8", errors="replace")
                    self.cache[url] = (text, response.headers.get("ETag", ""), response.headers.get("Last-Modified", ""))
                    return text
            raise ValueError("Source retries exhausted")


class SSAdapter:
    """Discover the actual SS.lv hierarchy and collect complete sale-only scopes."""

    def __init__(self, http: PublicHTTP, locations: dict[str, dict]):
        """Reuse a persistent location catalog across scans."""
        self.http = http
        self.locations = locations

    async def discover(self, path: str = "") -> list[dict]:
        """Load only the next level of public category links."""
        path = path.strip("/")
        if path and "ss:" + path not in self.locations:
            raise ValueError("Unknown SS.lv category")
        url = SS_ROOT + (path + "/" if path else "")
        html = await self.http.get(url)
        soup = BeautifulSoup(html, "html.parser")
        children = []
        prefix = urlsplit(url).path
        for anchor in soup.select("h4 a[href]"):
            href = urlsplit(urljoin(url, anchor["href"])).path
            suffix = href.removeprefix(prefix).strip("/")
            if not href.startswith(prefix) or not suffix or "/" in suffix or suffix in {"houses-abroad-latvia", "all"}:
                continue
            child_path = (path + "/" + suffix).strip("/")
            record = {"id": "ss:" + child_path, "label": anchor.get_text(" ", strip=True), "path": child_path, "parent": "ss:" + path if path else "", "group": self.locations.get("ss:" + path, {}).get("label", "Latvia")}
            self.locations[record["id"]] = record
            children.append(record)
        if not children and not soup.select_one("#head_line"):
            raise ValueError("SS.lv category hierarchy could not be verified")
        return children

    async def full_image(self, listing_url: str) -> str | None:
        """Retrieve a public advertisement's real gallery URL through the shared rate limit."""
        return parse_ss_image(await self.http.get(listing_url), listing_url)

    async def collect(self, path: str) -> list[Listing]:
        """Read every page in a selected sale scope; never baseline a partial scan."""
        base = SS_ROOT + (path.strip("/") + "/" if path else "") + "sell/"
        first = await self.http.get(base)
        soup = BeautifulSoup(first, "html.parser")
        pages = {base}
        for anchor in soup.select("a[href]"):
            target = urljoin(base, anchor["href"])
            if re.fullmatch(re.escape(base) + r"page\d+\.html", target):
                pages.add(target)
        if len(pages) > 200:
            raise ValueError("Source pagination exceeds safe scan limit")
        result = {listing.id: listing for listing in parse_ss(first, base, self.locations)}
        for page in sorted(pages - {base}):
            for listing in parse_ss(await self.http.get(page), page, self.locations):
                result[listing.id] = listing
        try:
            feed = feedparser.parse(await self.http.get(base + "rss/"))
            for entry in feed.entries:
                identifier = "ss:" + urlsplit(entry.get("link", "")).path.rsplit("/", 1)[-1].removesuffix(".html")
                if identifier in result and entry.get("published"):
                    result[identifier].published_at = parsedate_to_datetime(entry.published).isoformat()
        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError):
            pass
        return list(result.values())


def location_ids_for_text(text: str, locations: dict[str, dict]) -> list[str]:
    """Map exact address components to known SS.lv category IDs, including old municipality names."""
    components = {normalized(component) for component in text.split(",")}
    result = {key for key, record in locations.items() if normalized(record["label"]) in components}
    if components.intersection({"carnikavas novads", "carnikavas nov.", "carnikavas pagasts", "carnikavas pag."}):
        result.add("ss:riga-region/carnikavas-nov")
    if components.intersection({"mangalu pussala", "mangalsala"}):
        result.add("ss:riga/mangalsala")
    for location_id in list(result):
        path = location_id.removeprefix("ss:").split("/")
        result.update("ss:" + "/".join(path[:depth]) for depth in range(1, len(path)))
    return sorted(result)


def parse_city24(html: str, locations: dict[str, dict]) -> list[Listing]:
    """Parse verified rendered house-sale cards; use the explicit house area in image alternative text."""
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for card in soup.select(".object--result"):
        anchor = card.select_one('a[href*="/real-estate/houses-for-sale/"]')
        if anchor is None:
            continue
        url = urljoin("https://www.city24.lv", anchor["href"]).split("?", 1)[0]
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname != "www.city24.lv":
            continue
        identifier = urlsplit(url).path.rsplit("/", 1)[-1]
        if not identifier.isdigit():
            continue
        address = card.select_one(".object__address")
        region = card.select_one(".object__area")
        location = ", ".join(node.get_text(" ", strip=True) for node in [address, region] if node)
        images = card.select('img[alt*="Pārdod māju"]')
        image = next((candidate for candidate in images if candidate.get("src", "").startswith("https://static.img-city24.lv/")), images[0] if images else None)
        alt = image.get("alt", "") if image else ""
        area_match = re.search(r" - ([\d\s.,]+) m²(?:,| -)", alt)
        raw = area_match.group(1).strip() if area_match else ""
        area, status = parse_area(raw)
        price_match = re.search(r"([\d][\d\s.,]*\s*€)(?!/)", card.get_text(" ", strip=True))
        image_url = urljoin(url, image.get("src", "")) if image else None
        if image_url and "/placeholder/" in image_url:
            image_url = None
        slogan = card.select_one(".object__slogan")
        result.append(Listing("city24:" + identifier, "City24.lv", slogan.get_text(" ", strip=True) if slogan else location,
                              url, price_match.group(1).strip() if price_match else "Price on request", location,
                              location_ids_for_text(location, locations), area, raw, status, image_url))
    if not result:
        raise ValueError("City24 public house-sale cards missing or changed")
    return result


async def close_browser(browser, driver, timeout: float = 5):
    """Bound browser and driver cleanup so source cancellation can finish."""
    for resource, method in [(browser, "close"), (driver, "stop")]:
        if resource is not None:
            try:
                await asyncio.wait_for(getattr(resource, method)(), timeout=timeout)
            except Exception:
                pass


class City24Adapter:
    """Use ordinary rendered public pages without private API dependencies."""

    def __init__(self, http: PublicHTTP, locations: dict[str, dict], selected_locations: list[str] | None = None, cached_urls: list[str] | None = None):
        """Share robot rules and the canonical geography catalog."""
        self.http = http
        self.locations = locations
        self.selected_locations = selected_locations
        self.search_urls = cached_urls or []
        if not self.search_urls and selected_locations and all(location in CITY24_AREA_SEARCHES for location in selected_locations):
            self.search_urls = list(dict.fromkeys(CITY24_AREA_SEARCHES[location] for location in selected_locations))
        self.pages_checked = 0

    def location_labels(self) -> list[str]:
        """Translate canonical selected areas into public City24 picker labels."""
        if self.selected_locations is None:
            return []
        labels = []
        for location in self.selected_locations:
            label = self.locations.get(location, {}).get("label")
            if location == "ss:riga-region/carnikavas-nov":
                label = "Carnikavas pag."
            elif location == "ss:riga/mangalsala":
                label = "Mangaļu pussala"
            if not label:
                raise ValueError("Selected area is missing from the location catalog")
            labels.append(label)
        return list(dict.fromkeys(labels))

    def valid_search_url(self, url: str) -> bool:
        """Accept only public location-filtered house searches, never arbitrary URLs."""
        parsed = urlsplit(url)
        prefix = "/real-estate-search/houses-for-sale/"
        return parsed.scheme == "https" and parsed.hostname == "www.city24.lv" and not parsed.query and not parsed.fragment and parsed.path.startswith(prefix) and bool(re.search(r"/id=\d+-(?:district|parish|city|county)(?:,\d+-(?:district|parish|city|county))*$", parsed.path))

    async def resolve_searches(self, page, base: str) -> list[str]:
        """Resolve each area separately because City24 drops mixed-level selections."""
        from playwright.async_api import TimeoutError as BrowserTimeout

        if self.selected_locations is None:
            return [base]
        if self.search_urls:
            if not isinstance(self.search_urls, list) or not all(self.valid_search_url(url) for url in self.search_urls):
                raise ValueError("Cached City24 location search URL is invalid")
            return self.search_urls
        targets = []
        for label in self.location_labels():
            response = await page.goto(base, wait_until="domcontentloaded", timeout=45000)
            if response is None or response.status != 200:
                raise ValueError("City24 public location picker unavailable")
            field = page.locator('input[placeholder^="Meklēt pēc atrašanās"]')
            await field.wait_for(timeout=15000)
            reject = page.locator("#onetrust-reject-all-handler")
            if await reject.is_visible():
                await reject.click()
            await field.fill(label)
            option = page.locator("button.autocomplete__item").filter(has=page.get_by_text(re.compile("^" + re.escape(label) + "$", re.IGNORECASE)))
            try:
                await option.first.wait_for(timeout=10000)
            except BrowserTimeout as error:
                raise ValueError(f"City24 location picker could not resolve {label}; refusing an unfiltered crawl") from error
            if await option.count() != 1:
                raise ValueError(f"City24 location is ambiguous: {label}")
            value = await option.get_attribute("value")
            selection = re.fullmatch(r"address\.(district|parish|city|county)\|(\d+)", value or "")
            if not selection:
                raise ValueError("City24 geographic option is not a supported location ID")
            previous = page.url
            await option.click()
            await page.wait_for_url(lambda url: url != previous and "/id=" in url, timeout=15000)
            target = page.url.rstrip("/")
            token = selection.group(2) + "-" + selection.group(1)
            if not self.valid_search_url(target) or token not in target.rsplit("/id=", 1)[-1].split(","):
                raise ValueError("City24 did not retain the selected geographic ID")
            targets.append(target)
        self.search_urls = list(dict.fromkeys(targets))
        return self.search_urls

    async def collect_search(self, page, base: str) -> list[Listing]:
        """Read all pages for one verified area, without broadening the filter."""
        if not self.http.robots["https://www.city24.lv"].can_fetch(USER_AGENT, base):
            raise ValueError("City24 robots.txt disallows the selected location search")
        response = await page.goto(base, wait_until="domcontentloaded", timeout=45000)
        if response is None or response.status != 200:
            raise ValueError("City24 public page unavailable; no bypass attempted")
        first = await self.rendered_page(page)
        self.pages_checked += 1
        result = {listing.id: listing for listing in parse_city24(first, self.locations)}
        soup = BeautifulSoup(first, "html.parser")
        page_numbers = [int(found.group(1)) for anchor in soup.select("a[href]") if (found := re.fullmatch(re.escape(base) + r"/pg=(\d+)", urljoin(base, anchor["href"]))) is not None]
        maximum = max(page_numbers, default=1)
        if maximum > 200:
            raise ValueError("City24 pagination exceeds safe scan limit")
        for number in range(2, maximum + 1):
            target = base + f"/pg={number}"
            if not self.http.robots["https://www.city24.lv"].can_fetch(USER_AGENT, target):
                raise ValueError("City24 robots.txt disallows a required page")
            await asyncio.sleep(1)
            response = await page.goto(target, wait_until="domcontentloaded", timeout=45000)
            if response is None or response.status != 200:
                raise ValueError("City24 page failed; partial scan discarded")
            for listing in parse_city24(await self.rendered_page(page), self.locations):
                result[listing.id] = listing
            self.pages_checked += 1
        return list(result.values())

    async def collect(self) -> list[Listing]:
        """Collect all publicly paginated house sales and close Chromium after each scan."""
        from playwright.async_api import async_playwright

        root = "https://www.city24.lv/real-estate-search/houses-for-sale"
        await self.http.get(self.search_urls[0] if self.search_urls else root)
        driver = await async_playwright().start()
        browser = None
        try:
            browser = await driver.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                context = await browser.new_context(user_agent=USER_AGENT)
                page = await context.new_page()
                await page.route("**/*", self.route)
                targets = await self.resolve_searches(page, root)
                result = {}
                for index, target in enumerate(targets):
                    if index:
                        await asyncio.sleep(1)
                    for listing in await self.collect_search(page, target):
                        result[listing.id] = listing
                return list(result.values())
            finally:
                await close_browser(browser, None)
        finally:
            await close_browser(None, driver)

    async def rendered_page(self, page) -> str:
        """Wait for real cards and their lazy image URLs, without requiring photos to exist."""
        from playwright.async_api import TimeoutError as BrowserTimeout

        await page.locator('.object--result a[href*="/real-estate/houses-for-sale/"]').first.wait_for(timeout=45000)
        await page.locator(".object--result").first.scroll_into_view_if_needed()
        try:
            await page.locator('.object--result img[src^="https://static.img-city24.lv/"]').first.wait_for(timeout=5000)
        except BrowserTimeout:
            pass
        return await page.content()

    async def route(self, route):
        """Preserve lazy image URLs without downloading photos during collection."""
        if route.request.resource_type == "image":
            await route.fulfill(status=200, content_type="image/png", body=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a0ioAAAAASUVORK5CYII="))
        elif route.request.resource_type in {"media", "font"}:
            await route.abort()
        else:
            await route.continue_()


def parse_latio(html: str, locations: dict[str, dict]) -> list[Listing]:
    """Read house-sale facts from public cards, only when reuse permission is confirmed."""
    soup = BeautifulSoup(html, "html.parser")
    result = []
    if not soup.select(".results__item"):
        raise ValueError("Latio public listing cards missing or changed")
    for card in soup.select(".results__item"):
        description = card.get_text(" ", strip=True)
        if "Pirkt | Māja |" not in description:
            continue
        anchor = card.select_one("a.results__link[href]")
        if not anchor:
            continue
        url = urljoin("https://latio.lv", anchor["href"])
        if urlsplit(url).scheme != "https" or urlsplit(url).hostname != "latio.lv" or not re.fullmatch(r"/lv/ipasumi/\d+", urlsplit(url).path):
            continue
        area_icon = card.select_one('img[data-src*="ic_apartaments_area"]')
        area_node = area_icon.parent.select_one("span") if area_icon else None
        raw = area_node.get_text("", strip=True) if area_node else ""
        area, status = parse_area(raw)
        addresses = [node.get_text(" ", strip=True) for node in card.select(".results__title,.results__location")]
        location = next((address for address in addresses if "|" not in address), "Unknown location")
        price = card.select_one(".results__price")
        image = card.select_one('.results__slider-item img[data-src]')
        result.append(Listing("latio:" + urlsplit(url).path.rsplit("/", 1)[-1], "Latio.lv", "House for sale · " + location, url,
                              price.get_text(" ", strip=True) if price else "Price on request", location,
                              location_ids_for_text(location, locations), area, raw, status,
                              urljoin(url, image["data-src"]) if image else None))
    return result


class LatioAdapter:
    """Permission-gated public-page adapter."""

    def __init__(self, http: PublicHTTP, locations: dict[str, dict]):
        """Share the standard public HTTP transport and location mapping."""
        self.http = http
        self.locations = locations

    async def collect(self) -> list[Listing]:
        """Traverse public pagination; only house-sale cards are retained."""
        base = "https://latio.lv/lv/ipasumi"
        first = await self.http.get(base)
        soup = BeautifulSoup(first, "html.parser")
        maximum = max([int(found.group(1)) for anchor in soup.select(".pagination a[href]") if (found := re.search(r"\?p=(\d+)$", anchor["href"]))], default=1)
        if maximum > 200:
            raise ValueError("Latio pagination exceeds safe scan limit")
        result = {listing.id: listing for listing in parse_latio(first, self.locations)}
        for number in range(2, maximum + 1):
            for listing in parse_latio(await self.http.get(base + f"?p={number}"), self.locations):
                result[listing.id] = listing
        return list(result.values())