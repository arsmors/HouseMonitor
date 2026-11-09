"""Verify scheduled-mode UI against mocked API data without touching live storage."""

import asyncio
import sys

from playwright.async_api import async_playwright, expect


async def check():
    state = {
        "searches": [], "listings": [], "locations": [],
        "status": {
            "mode": "scheduled", "scanning": False, "next_scan": None, "interval": 3600,
            "sources": {}, "events": [], "queue": {}, "telegram": {"ready": False},
            "notifications_enabled": True,
        },
    }
    async with async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def api_route(route):
                if route.request.url.endswith("/api/state"):
                    await route.fulfill(json=state)
                else:
                    await route.fulfill(status=400, json={"error": "Unexpected API request during browser test"})
                    errors.append(route.request.url)

            await page.route("**/api/**", api_route)
            await page.goto(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8787/", wait_until="domcontentloaded")
            button = page.locator("[data-scan]")
            await expect(button).to_have_text("Hourly checks")
            await expect(button).to_be_disabled()
            await expect(button).to_have_attribute("title", "Checks run hourly in GitHub Actions. Use Run workflow there for an immediate check.")
            await page.get_by_role("button", name="Activity", exact=True).click()
            await expect(page.locator(".page-heading p")).to_contain_text("GitHub Actions")
            await expect(page.locator("[data-scan]")).to_be_disabled()
            await page.get_by_role("button", name="Settings", exact=True).click()
            await expect(page.get_by_text("Persistent hosted libSQL database")).to_be_visible()
            await page.get_by_role("button", name="Manage searches", exact=True).click()
            await page.get_by_role("button", name="New search", exact=True).first.click()
            await expect(page.get_by_role("dialog")).to_be_visible()
            await page.get_by_role("button", name="Close dialog", exact=True).click()
            assert not errors, errors
            print("Scheduled UI: hourly controls, workflow guidance, and search management verified")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(check())
