"""Run real browser interactions against the local live service, without Telegram sends."""

import asyncio
import json
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def check_refresh_timer():
    """Prove manual checks replace the two-hour idle timer without actually scanning sources."""
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            page = await browser.new_page()
            await page.clock.install()
            phase = {"checking": False, "reads": 0, "repriced": False}
            saved_state = None

            async def state_route(route):
                """Serve real listings with controlled scan lifecycle state."""
                nonlocal saved_state
                if saved_state is None:
                    response = await route.fetch()
                    saved_state = await response.json()
                data = json.loads(json.dumps(saved_state))
                phase["reads"] += 1
                if phase["repriced"]:
                    data["listings"][0]["price"] = "Updated test price"
                data["status"]["interval"] = 7200
                data["status"]["scanning"] = phase["checking"]
                data["status"]["sources"]["City24.lv"] = {"status": "scanning" if phase["checking"] else "ok"}
                await route.fulfill(json=data)

            async def scan_route(route):
                """Start a simulated UI check without writing to the live database."""
                phase["checking"] = True
                await route.fulfill(status=202, json={"started": True})

            await page.route("**/api/state", state_route)
            await page.route("**/api/scan", scan_route)
            await page.goto("http://localhost:8787/", wait_until="domcontentloaded")
            button = page.locator("[data-scan]").first
            await expect(button).to_contain_text("Check now")
            history = page.locator('.price-history').first
            await history.locator('summary').click()
            await page.evaluate("() => { window.originalCard = document.querySelector('.listing-card'); }")
            await button.click()
            await expect(button).to_contain_text("Checking City24.lv")
            assert await page.evaluate("() => window.originalCard === document.querySelector('.listing-card')")
            await expect(history).to_have_attribute("open", "")
            await page.clock.run_for(15000)
            assert await page.evaluate("() => window.originalCard === document.querySelector('.listing-card')")
            phase["checking"] = False
            await page.clock.run_for(15000)
            await expect(button).to_be_enabled(timeout=10000)
            await expect(button).to_contain_text("Check now")
            assert phase["reads"] >= 3
            assert await page.locator(".connection-error").count() == 0
            await expect(history).to_have_attribute("open", "")
            phase["repriced"] = True
            await page.clock.run_for(7200000)
            await expect(page.locator('.listing-price').first).to_contain_text("Updated test price")
            await expect(history).to_have_attribute("open", "")
            print(json.dumps({"manual_check_progress_polling": True, "checking_clears_without_reload": True, "source_interval": 7200}))
        finally:
            await browser.close()


async def check():
    """Verify search-independent browsing, original links and responsive layouts."""
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto("http://localhost:8787/", wait_until="networkidle")
            assert await page.get_by_role("button", name="My searches", exact=True).count() == 0
            assert await page.locator('[data-add-search], [data-nav="searches"]').count() == 0
            await page.locator(".listing-card").first.wait_for()
            await page.get_by_role("button", name="Activity", exact=True).click()
            await page.get_by_role("link", name="House Monitor home", exact=True).click()
            await expect(page.get_by_role("heading", name="Recent listings", exact=True)).to_be_visible()
            filter_state = await (await page.request.get("http://localhost:8787/api/state")).json()
            assert await page.locator(".listing-card").count() == len(filter_state["listings"])
            location_tag = page.locator(".location-tag[data-location-filter]").first
            tag_id = await location_tag.get_attribute("data-location-filter")
            expected_tag_ids = {listing["id"] for listing in filter_state["listings"] if tag_id in listing["location_ids"]}
            await location_tag.click()
            await expect(page.locator('.location-tag[aria-pressed="true"]')).to_have_attribute("data-location-filter", tag_id)
            visible_ids = await page.locator(".listing-card [data-listing]").evaluate_all("buttons => buttons.map(button => button.dataset.listing)")
            assert visible_ids and set(visible_ids).issubset(expected_tag_ids)
            await page.locator(".location-tag[data-clear-location]").click()
            location_filter = page.locator(".location-filter-trigger").first
            filter_id = await location_filter.get_attribute("data-location-filter")
            expected_filter_ids = {listing["id"] for listing in filter_state["listings"] if filter_id in listing["location_ids"]}
            await location_filter.click()
            await expect(page.locator('.location-tag[aria-pressed="true"]')).to_have_attribute("data-location-filter", filter_id)
            visible_ids = await page.locator(".listing-card [data-listing]").evaluate_all("buttons => buttons.map(button => button.dataset.listing)")
            assert visible_ids and set(visible_ids).issubset(expected_filter_ids)
            await page.locator(".location-tag[data-clear-location]").click()
            original = await page.locator(".telegram-open").get_attribute("href")
            assert original.startswith("https://") and "/msg/" in original or "/real-estate/houses-for-sale/" in original or "/lv/ipasumi/" in original
            photo = page.locator(".listing-image").first
            photo_url = await photo.get_attribute("href")
            assert await photo.get_attribute("target") == "_blank"
            assert "noopener" in await photo.get_attribute("rel")
            async with page.expect_popup() as popup_info:
                await photo.click()
            popup = await popup_info.value
            await popup.wait_for_url(photo_url + "**", wait_until="domcontentloaded")
            await popup.close()
            await page.locator('.listing-card button[data-listing]').first.click()
            assert await page.locator(".telegram-open").get_attribute("href") == photo_url
            screenshots = Path("data/screenshots")
            screenshots.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshots / "desktop.png"))
            assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth")
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path=str(screenshots / "mobile.png"))
            assert not await page.evaluate("document.documentElement.scrollWidth > innerWidth")
            assert not errors, errors
            print(json.dumps({"all_saved_listings": True, "search_tab_removed": True, "original_ad_url": original, "photo_opens_original_in_new_tab": True, "separate_preview_works": True, "desktop_mobile_overflow": False, "console_errors": errors}))
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(check_refresh_timer() if "--refresh-only" in sys.argv else check())