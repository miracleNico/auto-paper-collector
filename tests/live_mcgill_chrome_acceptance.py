"""Validate one licensed article through a user-authenticated McGill Chrome session.

Authentication and 2FA are deliberately never automated.  The script reuses
the dedicated persistent Chrome profile, discovers a publisher PDF endpoint,
downloads one article, validates its identity, attaches it to the existing
Zotero record, and repeats the write to verify attachment idempotency.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from paper_endnote.clients import mcgill_proxy_url
from paper_endnote.config import Settings
from paper_endnote.db import Database
from paper_endnote.pdf_validation import validate_pdf
from paper_endnote.zotero import ZoteroAdapter


LOGIN_HOST_MARKERS = (
    "login.microsoftonline.com",
    "shibboleth",
    "saml",
    "idp.mcgill.ca",
)


def is_login_url(url: str) -> bool:
    folded = url.casefold()
    return any(marker in folded for marker in LOGIN_HOST_MARKERS)


async def candidate_links(page: Page) -> list[dict[str, str]]:
    values: list[dict[str, str]] = await page.locator("a[href]").evaluate_all(
        """links => links.map(link => ({
            text: (link.innerText || link.getAttribute('aria-label') || '').trim(),
            href: link.href || ''
        })).filter(value => value.href)"""
    )
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        text = value["text"].strip()
        href = value["href"].strip()
        haystack = f"{text} {href}".casefold()
        if not any(token in haystack for token in ("pdf", "pdfft", "download")):
            continue
        absolute = urljoin(page.url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        result.append({"text": text[:160], "href": absolute})
    # ScienceDirect's current article shell may render the PDF action as a
    # JavaScript control instead of an anchor.  Its canonical article PDF
    # endpoint is still derived from the PII in the authenticated page URL.
    parsed = urlparse(page.url)
    science_direct = re.search(r"/science/article/pii/([^/?#]+)", parsed.path, re.IGNORECASE)
    if science_direct:
        pii = science_direct.group(1)
        pdfft = f"{parsed.scheme}://{parsed.netloc}/science/article/pii/{pii}/pdfft?isDTMRedir=true&download=true"
        if pdfft not in seen:
            result.insert(0, {"text": "ScienceDirect PDF", "href": pdfft})
    return result


def rank_candidate(value: dict[str, str]) -> tuple[int, int]:
    haystack = f"{value['text']} {value['href']}".casefold()
    score = 0
    if "view pdf" in haystack or "download pdf" in haystack:
        score += 100
    if "/pdfft" in haystack or ".pdf" in haystack:
        score += 80
    if "article" in haystack:
        score += 10
    if "supp" in haystack or "support" in haystack:
        score -= 100
    return score, -len(value["href"])


async def fetch_pdf(context: BrowserContext, candidates: list[dict[str, str]], destination: Path) -> dict[str, Any]:
    errors: list[str] = []
    queue = sorted(candidates, key=rank_candidate, reverse=True)
    visited: set[str] = set()
    while queue:
        candidate = queue.pop(0)
        parsed_candidate = urlparse(candidate["href"])
        if parsed_candidate.scheme not in {"http", "https"} or candidate["href"] in visited:
            continue
        visited.add(candidate["href"])
        try:
            response = await context.request.get(
                candidate["href"],
                headers={"Accept": "application/pdf,text/html;q=0.8,*/*;q=0.5"},
                timeout=60_000,
                fail_on_status_code=False,
            )
            content_type = response.headers.get("content-type", "")
            body = await response.body()
            if response.status == 200 and body.startswith(b"%PDF-"):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(body)
                return {
                    "source_url": candidate["href"],
                    "link_text": candidate["text"],
                    "status": response.status,
                    "content_type": content_type,
                    "bytes": len(body),
                }
            if response.status == 200 and "html" in content_type.casefold():
                markup = html.unescape(body.decode("utf-8", errors="ignore")).replace("\\/", "/")
                discovered: list[str] = []
                for pattern in (
                    r"(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]",
                    r"https?://[^'\"<>\s]+",
                    r"['\"]([^'\"]+\.pdf(?:\?[^'\"]*)?)['\"]",
                ):
                    discovered.extend(re.findall(pattern, markup, flags=re.IGNORECASE))
                for value in discovered:
                    absolute = urljoin(candidate["href"], value)
                    haystack = absolute.casefold()
                    if absolute not in visited and any(token in haystack for token in (".pdf", "pdf", "stamp")):
                        queue.append({"text": "Embedded PDF", "href": absolute})
            errors.append(
                f"{response.status} {content_type} {candidate['href']} ({len(body)} bytes)"
            )
        except Exception as exc:
            errors.append(f"{candidate['href']}: {exc}")
    raise RuntimeError("No candidate returned a PDF: " + " | ".join(errors[:8]))


async def zotero_pdf_count(adapter: ZoteroAdapter, item_key: str) -> tuple[int, list[str]]:
    children = await adapter._children(item_key)
    pdfs = [item for item in children if item.get("data", item).get("contentType") == "application/pdf"]
    keys = [item.get("data", item).get("key") or item.get("key") for item in pdfs]
    return len(pdfs), keys


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings.load()
    doi = args.doi.casefold()
    target = f"https://doi.org/{doi}"
    entry_url = mcgill_proxy_url(target)
    destination = args.output_dir.resolve() / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', doi)}.pdf"

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(settings.browser_profile_dir),
            channel="chrome",
            headless=False,
            accept_downloads=True,
        )
        try:
            page = await context.new_page()
            await page.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(5000)
            if is_login_url(page.url):
                raise RuntimeError(f"McGill authentication is still required: {page.url}")
            title = await page.title()
            candidates = await candidate_links(page)
            if not candidates:
                raise RuntimeError(f"No PDF-like links found on {page.url} ({title})")
            retrieval = await fetch_pdf(context, candidates, destination)
        finally:
            await context.close()

    validation = validate_pdf(destination, expected_doi=doi, expected_title=args.title)
    if not (validation.valid_pdf and validation.identity == "verified" and validation.role == "main"):
        raise RuntimeError(f"Downloaded file did not validate: {validation.as_dict()}")

    database = Database(settings.database_path)
    adapter = ZoteroAdapter(database.get_setting("zotero_api_key", ""))
    try:
        collection_key = await adapter.ensure_collection(args.collection, create=False)
        metadata = {"doi": doi, "title": args.title, "authors": [], "year": args.year, "journal": ""}
        first = await adapter.commit_paper(collection_key, metadata, destination)
        before_repeat_count, before_repeat_keys = await zotero_pdf_count(adapter, first["record_number"])
        second = await adapter.commit_paper(collection_key, metadata, destination)
        after_repeat_count, after_repeat_keys = await zotero_pdf_count(adapter, second["record_number"])
    finally:
        await adapter.close()

    result = {
        "passed": bool(
            first["record_number"] == second["record_number"]
            and before_repeat_count == after_repeat_count
            and before_repeat_keys == after_repeat_keys
            and after_repeat_count >= 1
        ),
        "doi": doi,
        "entry_url": entry_url,
        "publisher_url": page.url,
        "publisher_title": title,
        "retrieval": retrieval,
        "validation": validation.as_dict(),
        "zotero": {
            "record_key": first["record_number"],
            "first_commit": first,
            "second_commit": second,
            "pdf_count_before_repeat": before_repeat_count,
            "pdf_count_after_repeat": after_repeat_count,
            "pdf_keys_before_repeat": before_repeat_keys,
            "pdf_keys_after_repeat": after_repeat_keys,
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doi", default="10.1016/j.adhoc.2023.103307")
    parser.add_argument(
        "--title",
        default="Delay/Disruption-Tolerant Networking-based the Integrated Deep-Space Relay Network: State-of-the-Art",
    )
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--collection", default="DTN")
    parser.add_argument("--output-dir", type=Path, default=Path("work/mcgill-chrome"))
    parser.add_argument("--report", type=Path, default=Path("outputs/mcgill-chrome-live-acceptance.json"))
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
