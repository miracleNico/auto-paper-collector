"""Print non-sensitive controls from the authenticated McGill publisher page."""

from __future__ import annotations

import asyncio
import argparse
import json

from playwright.async_api import async_playwright

from paper_endnote.clients import mcgill_proxy_url
from paper_endnote.config import Settings


async def inspect(url: str, wait_seconds: float) -> None:
    settings = Settings.load()
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_profile_dir),
            channel="chrome",
            headless=False,
            accept_downloads=True,
        )
        try:
            page = await context.new_page()
            network: list[dict[str, object]] = []
            captured_responses = []
            page.on(
                "response",
                lambda response: (
                    network.append({"status": response.status, "url": response.url}),
                    captured_responses.append(response),
                )
                if any(
                    token in response.url.casefold()
                    for token in ("thirdiron", "libkey", "sciencedirect", "worldcat")
                )
                else None,
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(wait_seconds * 1000)
            controls = await page.locator("a, button, [role=button]").evaluate_all(
                """nodes => nodes.map(node => ({
                    tag: node.tagName,
                    text: (node.innerText || node.getAttribute('aria-label') || node.title || '').trim(),
                    href: node.href || node.getAttribute('href') || '',
                    disabled: !!node.disabled
                })).filter(value => /pdf|download|view|access|institution|full.?text|elsevier|science.?direct/i.test(value.text + ' ' + value.href))"""
            )
            payloads: list[dict[str, object]] = []
            for response in captured_responses:
                if "public-api.thirdiron.com/public/v1/libraries/61/articles/" not in response.url:
                    continue
                try:
                    payloads.append({"url": response.url, "body": await response.json()})
                except Exception as exc:
                    payloads.append({"url": response.url, "error": str(exc)})
            print(
                json.dumps(
                    {
                        "url": page.url,
                        "title": await page.title(),
                        "frames": [frame.url for frame in page.frames],
                        "controls": controls[:50],
                        "network": network[-80:],
                        "payloads": payloads,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            await context.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default=mcgill_proxy_url("https://doi.org/10.1016/j.adhoc.2023.103307"),
    )
    parser.add_argument("--wait", type=float, default=15.0)
    args = parser.parse_args()
    asyncio.run(inspect(args.url, args.wait))


if __name__ == "__main__":
    main()
