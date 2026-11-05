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
            phase = {"checking": False, "reads": 0}

            async def state_route(route):
                """Serve real listings with controlled scan lifecycle state."""
                response = await route.fetch()
                data = await response.json()
                phase["reads"] += 1
                data["status"]["interval"] = 7200
                data["status"]["scanning"] = phase["checking"]
                data["status"]["sources"]["City24.lv"] = {"status": "scanning" if phase["checking"] else "ok"}
                await route.fulfill(response=response, json=data)

            async def scan_route(route):
                """Start a simulated UI check without writing to the live database."""
                phase["checking"] = True
                await route.fulfill(status=202, json={"started": True})

            await page.route("**/api/state", state_route)
            await page.route("**/api/scan", scan_route)
            await page.goto("http://localhost:8787/", wait_until="domcontentloaded")
            button = page.locator("[data-scan]").first
            await expect(button).to_contain_text("Check now")
            await button.click()
            await expect(button).to_contain_text("Checking City24.lv")
            phase["checking"] = False
            await page.clock.run_for(15000)
            await expect(button).to_be_enabled(timeout=10000)
            await expect(button).to_contain_text("Check now")
            assert phase["reads"] >= 3
            assert await page.locator(".connection-error").count() == 0
            print(json.dumps({"manual_check_progress_polling": True, "checking_clears_without_reload": True, "source_interval": 7200}))
        finally:
            await browser.close()


async def check():
    """Verify live search actions, persistence, original links and responsive layouts."""
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        name = f"Browser verification {int(time.time())}"
        search_id = None
        try:
            await page.goto("http://localhost:8787/", wait_until="networkidle")
            await page.get_by_role("button", name="My searches", exact=True).click()
            await page.locator("#new-search").click()
            await page.get_by_label("Search name").fill(name)
            await page.get_by_label("Minimum area").fill("100")
            await page.get_by_label("Maximum area").fill("200")
            await page.get_by_role("button", name="Continue", exact=True).click()
            await page.locator('input[name="location"][value="ss:riga/vecaki"]').check()
            await page.get_by_role("button", name="Continue", exact=True).click()
            await page.get_by_role("button", name="Save search", exact=True).click()
            await page.get_by_role("heading", name=name, exact=True).wait_for()
            response = await page.request.get("http://localhost:8787/api/state")
            state = await response.json()
            saved = next(search for search in state["searches"] if search["name"] == name)
            search_id = saved["id"]
            assert saved["min"] == 100 and saved["max"] == 200 and saved["locations"] == ["ss:riga/vecaki"]
            await page.get_by_role("button", name=f"Pause {name}", exact=True).click()
            await page.get_by_role("button", name=f"Resume {name}", exact=True).wait_for()
            await page.get_by_role("button", name=f"Resume {name}", exact=True).click()
            await page.get_by_role("button", name=f"Pause {name}", exact=True).wait_for()
            await page.get_by_role("button", name=f"Edit {name}", exact=True).click()
            await page.get_by_label("Search name").fill(name + " edited")
            await page.get_by_role("button", name="Continue", exact=True).click()
            await page.get_by_role("button", name="Continue", exact=True).click()
            await page.get_by_role("button", name="Save search", exact=True).click()
            name += " edited"
            await page.get_by_role("heading", name=name, exact=True).wait_for()
            await page.get_by_role("button", name=f"Delete {name}", exact=True).click()
            await page.get_by_role("button", name="Delete search", exact=True).click()
            await page.get_by_role("heading", name=name, exact=True).wait_for(state="detached")
            search_id = None
            await page.get_by_role("button", name="Recent listings", exact=True).click()
            await page.reload(wait_until="networkidle")
            await page.locator(".listing-card").first.wait_for()
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
            await page.get_by_role("button", name="My searches", exact=True).click()
            await page.locator("#new-search").click()
            assert not await page.locator("dialog").evaluate("element => element.scrollWidth > element.clientWidth")
            await page.get_by_role("button", name="Close dialog", exact=True).click()
            assert not errors, errors
            print(json.dumps({"create": True, "edit": True, "pause_resume": True, "delete": True, "sqlite_persistence": True, "original_ad_url": original, "photo_opens_original_in_new_tab": True, "separate_preview_works": True, "desktop_mobile_overflow": False, "console_errors": errors}))
        finally:
            if search_id is not None:
                await page.request.delete(f"http://localhost:8787/api/searches/{search_id}", headers={"X-House-Monitor": "1"})
            await browser.close()


if __name__ == "__main__":
    asyncio.run(check_refresh_timer() if "--refresh-only" in sys.argv else check())