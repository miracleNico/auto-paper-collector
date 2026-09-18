from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .inputs import normalize_doi
from .user_config import InstitutionProfile, institution_openurl, institution_proxy_url, load_preset


class RemoteServiceError(RuntimeError):
    pass


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
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    def _user_agent(self) -> str:
        suffix = f"; mailto:{self.settings.crossref_mailto}" if self.settings.crossref_mailto else ""
        return f"PaperEndNote/0.1 (local research workflow{suffix})"

    async def _get(self, url: str, **params: Any) -> dict[str, Any]:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            delay = self.settings.crossref_min_interval_seconds - (now - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            response = await self.client.get(url, params=params)
            self._last_request = asyncio.get_running_loop().time()
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
