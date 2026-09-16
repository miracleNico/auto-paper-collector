from __future__ import annotations

import re
from urllib.parse import urlparse


ARNUMBER_RE = re.compile(r"(?:arnumber=|/document/)(\d+)", re.IGNORECASE)
IELX_RE = re.compile(r"(/ielx\d+/[^?#]+\.pdf)", re.IGNORECASE)
PII_RE = re.compile(r"/science/article/pii/([^/?#]+)", re.IGNORECASE)
BLOCK_TOKENS = ("ip blocked", "captcha", "recaptcha")


def _ieee_host(hostname: str | None) -> bool:
    folded = (hostname or "").casefold()
    return "ieee" in folded or "ieeexplore" in folded


def is_publisher_block(status: int, body: bytes, sample: str = "") -> bool:
    """True when a publisher response is a hard 403 / IP / captcha block, not a PDF."""
    if body.startswith(b"%PDF-"):
        return False
    if status == 403:
        return True
    folded = sample.casefold()
    return any(token in folded for token in BLOCK_TOKENS)


def publisher_pdf_candidates(page_url: str) -> list[dict[str, str]]:
    """Return publisher-specific PDF URLs derived from the current article page."""
    parsed = urlparse(page_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return []
    origin = f"{parsed.scheme}://{parsed.netloc}"
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(text: str, href: str) -> None:
        if href not in seen:
            seen.add(href)
            results.append({"text": text, "href": href})

    if _ieee_host(parsed.hostname):
        match = ARNUMBER_RE.search(page_url)
        if match:
            arnumber = match.group(1)
            add(
                "IEEE stampPDF",
                f"{origin}/stampPDF/getPDF.jsp?tp=&arnumber={arnumber}&ref=",
            )
            add(
                "IEEE stamp",
                f"{origin}/stamp/stamp.jsp?tp=&arnumber={arnumber}",
            )
        ielx = IELX_RE.search(parsed.path)
        if ielx:
            add("IEEE ielx", f"{origin}{ielx.group(1)}")

    pii = PII_RE.search(parsed.path)
    if pii:
        add(
            "ScienceDirect PDF",
            f"{origin}/science/article/pii/{pii.group(1)}/pdfft?isDTMRedir=true&download=true",
        )
    return results
