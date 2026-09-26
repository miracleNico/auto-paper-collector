from __future__ import annotations

import asyncio
import html
import math
import re
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .inputs import normalize_doi
from .user_config import InstitutionProfile, institution_openurl, institution_proxy_url, load_preset


class RemoteServiceError(RuntimeError):
    pass


class _CrossrefBudget:
    """One event loop's shared Crossref connection and dispatch budget."""

    def __init__(self) -> None:
        self.public = asyncio.Semaphore(1)
        self.polite = asyncio.Semaphore(3)
        self.dispatch_lock = asyncio.Lock()
        self.next_send = {"doi": 0.0, "search": 0.0}
        self.minimum_intervals = {"doi": 0.0, "search": 0.0}
        self.blocked_until = 0.0

    async def wait_to_send(self, kind: str, configured_interval: float) -> None:
        loop = asyncio.get_running_loop()
        while True:
            async with self.dispatch_lock:
                now = loop.time()
                if self.blocked_until > now:
                    delay = self.blocked_until - now
                else:
                    interval = max(
                        configured_interval,
                        self.minimum_intervals[kind],
                        1.0 if kind == "search" else 0.0,
                    )
                    send_at = max(now, self.next_send[kind])
                    self.next_send[kind] = send_at + interval
                    delay = send_at - now
                    break
            await asyncio.sleep(min(delay, 60.0))
        while True:
            delay = send_at - loop.time()
            if delay <= 0:
                break
            await asyncio.sleep(min(delay, 60.0))
        # A 429 could arrive while a request is waiting for its reserved slot.
        while True:
            async with self.dispatch_lock:
                delay = self.blocked_until - loop.time()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 60.0))

    async def observe(self, response: httpx.Response, retry_delay: float = 0.0) -> None:
        interval = _crossref_header_interval(response)
        async with self.dispatch_lock:
            if interval is not None:
                for kind in self.minimum_intervals:
                    self.minimum_intervals[kind] = max(self.minimum_intervals[kind], interval)
            if retry_delay:
                self.blocked_until = max(
                    self.blocked_until, asyncio.get_running_loop().time() + retry_delay
                )


_crossref_budgets: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _CrossrefBudget] = weakref.WeakKeyDictionary()


def _crossref_budget() -> _CrossrefBudget:
    loop = asyncio.get_running_loop()
    budget = _crossref_budgets.get(loop)
    if budget is None:
        budget = _CrossrefBudget()
        _crossref_budgets[loop] = budget
    return budget


def _crossref_header_interval(response: httpx.Response) -> float | None:
    try:
        limit = int(response.headers["x-rate-limit-limit"])
        raw_interval = response.headers["x-rate-limit-interval"].strip().casefold()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)?", raw_interval)
        if limit <= 0 or not match:
            return None
        multiplier = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[match.group(2) or "s"]
        return float(match.group(1)) * multiplier / limit
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("retry-after", "").strip()
    try:
        if raw:
            seconds = float(raw)
            if math.isfinite(seconds):
                return max(0.0, seconds)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return float(min(2**attempt, 8))


def _year(work: dict[str, Any]) -> int | None:
    for field in ("published-print", "published-online", "published", "issued", "created"):
        parts = work.get(field, {}).get("date-parts", []) if isinstance(work.get(field), dict) else []
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                pass
    return None


def normalize_crossref_work(work: dict[str, Any], score: float | None = None) -> dict[str, Any]:
    authors = []
    for author in work.get("author", []) or []:
        family = str(author.get("family", "")).strip()
        given = str(author.get("given", "")).strip()
        name = ", ".join(part for part in (family, given) if part)
        if name:
            authors.append(name)
    title_values = work.get("title") or []
    container = work.get("container-title") or []
    doi = normalize_doi(work.get("DOI"))
    return {
        "doi": doi,
        "title": html.unescape(str(title_values[0])).strip() if title_values else "",
        "year": _year(work),
        "authors": authors,
        "journal": html.unescape(str(container[0])).strip() if container else "",
        "volume": str(work.get("volume", "") or ""),
        "issue": str(work.get("issue", "") or ""),
        "pages": str(work.get("page", "") or ""),
        "publisher": str(work.get("publisher", "") or ""),
        "type": str(work.get("type", "journal-article") or "journal-article"),
        "url": str(work.get("URL", "") or ""),
        "score": score if score is not None else work.get("score"),
    }


class CrossrefClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        headers = {"User-Agent": self._user_agent()}
        self.client = httpx.AsyncClient(headers=headers, timeout=settings.request_timeout_seconds, follow_redirects=True)

    def _user_agent(self) -> str:
        suffix = f"; mailto:{self.settings.crossref_mailto}" if self.settings.crossref_mailto else ""
        return f"PaperEndNote/0.1 (local research workflow{suffix})"

    async def _get(self, url: str, **params: Any) -> dict[str, Any]:
        budget = _crossref_budget()
        kind = "search" if url.rstrip("/").endswith("/works") else "doi"
        semaphore = budget.polite if self.settings.crossref_mailto else budget.public
        for attempt in range(3):
            async with semaphore:
                await budget.wait_to_send(kind, self.settings.crossref_min_interval_seconds)
                response = await self.client.get(
                    url, params=params, headers={"User-Agent": self._user_agent()}
                )
                retry_delay = _retry_after_seconds(response, attempt) if response.status_code == 429 else 0.0
                await budget.observe(response, retry_delay)
            if response.status_code != 429 or attempt == 2:
                break
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RemoteServiceError(f"Crossref returned HTTP {response.status_code}") from exc
        return response.json()

    async def by_doi(self, doi: str) -> dict[str, Any]:
        encoded = quote(doi, safe="")
        payload = await self._get(f"https://api.crossref.org/works/{encoded}")
        return normalize_crossref_work(payload["message"])

    async def search(self, title: str, *, rows: int = 5) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query.bibliographic": title, "rows": rows, "select": "DOI,title,author,container-title,published,published-print,published-online,issued,created,volume,issue,page,publisher,type,URL,score"}
        if self.settings.crossref_mailto:
            params["mailto"] = self.settings.crossref_mailto
        payload = await self._get("https://api.crossref.org/works", **params)
        items = payload.get("message", {}).get("items", [])
        return [normalize_crossref_work(item, item.get("score")) for item in items]

    async def close(self) -> None:
        await self.client.aclose()


@dataclass(frozen=True)
class OALocation:
    url: str
    pdf_url: str | None
    landing_url: str | None
    version: str
    host_type: str
    license: str | None


class UnpaywallClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=settings.request_timeout_seconds, follow_redirects=True)

    async def best_location(self, doi: str) -> OALocation | None:
        if not self.settings.unpaywall_email:
            return None
        encoded = quote(doi, safe="")
        response = await self.client.get(
            f"https://api.unpaywall.org/v2/{encoded}", params={"email": self.settings.unpaywall_email}
        )
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RemoteServiceError(f"Unpaywall returned HTTP {response.status_code}") from exc
        location = response.json().get("best_oa_location")
        if not location:
            return None
        pdf_url = location.get("url_for_pdf")
        landing_url = location.get("url_for_landing_page")
        return OALocation(
            url=pdf_url or landing_url or location.get("url", ""),
            pdf_url=pdf_url,
            landing_url=landing_url,
            version=location.get("version") or "unknown",
            host_type=location.get("host_type") or "unknown",
            license=location.get("license"),
        )

    async def close(self) -> None:
        await self.client.aclose()


def scholar_search_url(title_or_doi: str) -> str:
    return f"https://scholar.google.com/scholar?q={quote(title_or_doi)}"


def mcgill_profile() -> InstitutionProfile:
    return load_preset("mcgill")


def mcgill_proxy_url(target_url: str) -> str:
    return institution_proxy_url(mcgill_profile(), target_url)


def mcgill_worldcat_url(doi: str) -> str:
    return institution_openurl(mcgill_profile(), doi) or ""


def likely_pdf_url(url: str) -> bool:
    return bool(re.search(r"\.pdf(?:$|[?#])", url, flags=re.IGNORECASE))
