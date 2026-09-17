from __future__ import annotations

import re
from urllib.parse import urlparse


ARNUMBER_RE = re.compile(r"(?:arnumber=|/document/)(\d+)", re.IGNORECASE)
IELX_RE = re.compile(r"(/ielx\d+/[^?#]+\.pdf)", re.IGNORECASE)
PII_RE = re.compile(r"/science/article/pii/([^/?#]+)", re.IGNORECASE)
BLOCK_TOKENS = ("ip blocked", "captcha", "recaptcha")


def _host_is_domain(hostname: str, domain: str) -> bool:
    hostname = hostname.rstrip(".").casefold()
    domain = domain.rstrip(".").casefold()
    return hostname == domain or hostname.endswith(f".{domain}")


def publisher_key(page_url: str) -> str:
    """Return the stable key used by institution-login configuration and state."""
    host = (urlparse(page_url).hostname or "").casefold()
    if _host_is_domain(host, "ieee.org"):
        return "ieee"
    if _host_is_domain(host, "sciencedirect.com") or _host_is_domain(
        host, "elsevier.com"
    ):
        return "sciencedirect"
    if not host or host in {"doi.org", "dx.doi.org", "scholar.google.com"}:
        return "generic"
    # Unknown and proxy-rewritten hosts keep distinct session/failure buckets.
    # Do not infer a publisher from a substring: ieee-login.evil.example must
    # never activate an official-site adapter.
    return host.removeprefix("www.")


def publisher_institution_login_candidates(
    page_url: str, links: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Rank visible institution-login links without following third-party URLs."""
    publisher = publisher_key(page_url)
    if publisher not in {"ieee", "sciencedirect"}:
        return []
    ranked: list[tuple[int, dict[str, str]]] = []
    seen: set[str] = set()
    for link in links:
        href = str(link.get("href") or "").strip()
        text = str(link.get("text") or "").strip()
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or href in seen:
            continue
        candidate_host = (parsed.hostname or "").casefold()
        if publisher == "ieee" and not _host_is_domain(candidate_host, "ieee.org"):
            continue
        if publisher == "sciencedirect" and not any(
            _host_is_domain(candidate_host, domain)
            for domain in ("sciencedirect.com", "elsevier.com")
        ):
            continue
        haystack = f"{text} {href}".casefold()
        score = 0
        if any(token in haystack for token in ("institutional sign in", "institution sign in")):
            score += 120
        if any(token in haystack for token in ("access through your institution", "sign in via your institution")):
            score += 110
        if any(token in haystack for token in ("institution", "shibboleth", "saml", "federation", "carsi")):
            score += 60
        if publisher == "ieee" and any(token in haystack for token in ("instsignin", "institutional-sign-in")):
            score += 80
        if publisher == "sciencedirect" and any(token in haystack for token in ("institution", "federated")):
            score += 50
        if any(token in haystack for token in ("personal", "register", "create account")):
            score -= 120
        if score > 0:
            seen.add(href)
            ranked.append((score, {"text": text[:160], "href": href}))
    ranked.sort(key=lambda item: (item[0], -len(item[1]["href"])), reverse=True)
    return [item for _, item in ranked]


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
