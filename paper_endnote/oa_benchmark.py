from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

import httpx

from .config import Settings
from .inputs import normalize_doi, parse_input, title_similarity


ALLOWED_METADATA_HOSTS = frozenset(
    {
        "api.unpaywall.org",
        "api.openalex.org",
        "api.core.ac.uk",
        "www.ebi.ac.uk",
        "export.arxiv.org",
    }
)

PROVIDER_INTERVALS = {
    "unpaywall": 0.1,
    "openalex": 0.1,
    "core": 2.1,
    "europe_pmc": 0.2,
    "arxiv": 3.1,
}


class CandidateDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class MetadataPayload:
    status_code: int
    content_type: str
    text: str


class CandidateOnlyHTTP:
    """HTTP transport restricted to known metadata endpoints.

    Candidate PDF and landing URLs are returned as data. They are never passed to
    this transport, which makes the candidate-only guarantee testable.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        user_agent: str,
        timeout_seconds: float = 30.0,
        cache_ttl_seconds: float = 7 * 24 * 60 * 60,
        intervals: dict[str, float] | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.intervals = {**PROVIDER_INTERVALS, **(intervals or {})}
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self.stats: dict[str, Any] = {
            "metadata_network_requests": 0,
            "metadata_cache_hits": 0,
            "candidate_url_requests": 0,
            "network_requests_by_provider": Counter(),
            "cache_hits_by_provider": Counter(),
        }

    @staticmethod
    def _validate_endpoint(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in ALLOWED_METADATA_HOSTS:
            raise CandidateDiscoveryError(f"Refusing non-metadata endpoint: {url}")

    @staticmethod
    def _cache_key(provider: str, url: str, params: dict[str, Any]) -> str:
        canonical = json.dumps(
            [provider, url, sorted((str(key), str(value)) for key, value in params.items())],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _read_cache(self, path: Path) -> MetadataPayload | None:
        if not path.is_file():
            return None
        age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        if age > self.cache_ttl_seconds:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return MetadataPayload(
                status_code=int(value["status_code"]),
                content_type=str(value.get("content_type", "")),
                text=str(value.get("text", "")),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _write_cache(path: Path, payload: MetadataPayload) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "status_code": payload.status_code,
                    "content_type": payload.content_type,
                    "text": payload.text,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    async def get(
        self,
        provider: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str,
    ) -> MetadataPayload:
        self._validate_endpoint(url)
        request_params = params or {}
        cache_path = self.cache_dir / f"{self._cache_key(provider, url, request_params)}.json"
        cached = self._read_cache(cache_path)
        if cached is not None:
            self.stats["metadata_cache_hits"] += 1
            self.stats["cache_hits_by_provider"][provider] += 1
            return cached

        lock = self._locks.setdefault(provider, asyncio.Lock())
        async with lock:
            cached = self._read_cache(cache_path)
            if cached is not None:
                self.stats["metadata_cache_hits"] += 1
                self.stats["cache_hits_by_provider"][provider] += 1
                return cached

            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last_request.get(provider, 0.0)
            delay = self.intervals.get(provider, 0.0) - elapsed
            if delay > 0:
                await asyncio.sleep(delay)

            response: httpx.Response | None = None
            for attempt in range(3):
                response = await self._client.get(url, params=request_params, headers={"Accept": accept})
                self.stats["metadata_network_requests"] += 1
                self.stats["network_requests_by_provider"][provider] += 1
                self._last_request[provider] = loop.time()
                if response.status_code != 429 or attempt == 2:
                    break
                retry_after = response.headers.get("Retry-After", "")
                try:
                    retry_delay = max(float(retry_after), self.intervals.get(provider, 1.0))
                except ValueError:
                    retry_delay = self.intervals.get(provider, 1.0)
                await asyncio.sleep(min(max(retry_delay, 0.1), 30.0))

            assert response is not None
            payload = MetadataPayload(
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                text=response.text,
            )
            if response.status_code == 404:
                self._write_cache(cache_path, payload)
                return payload
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise CandidateDiscoveryError(
                    f"{provider} returned HTTP {response.status_code}"
                ) from exc
            self._write_cache(cache_path, payload)
            return payload

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def serializable_stats(self) -> dict[str, Any]:
        return {
            "metadata_network_requests": self.stats["metadata_network_requests"],
            "metadata_cache_hits": self.stats["metadata_cache_hits"],
            "candidate_url_requests": self.stats["candidate_url_requests"],
            "network_requests_by_provider": dict(self.stats["network_requests_by_provider"]),
            "cache_hits_by_provider": dict(self.stats["cache_hits_by_provider"]),
        }


def _json_payload(payload: MetadataPayload, provider: str) -> dict[str, Any]:
    if payload.status_code == 404:
        return {}
    try:
        value = json.loads(payload.text)
    except json.JSONDecodeError as exc:
        raise CandidateDiscoveryError(f"{provider} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise CandidateDiscoveryError(f"{provider} returned an unexpected JSON value")
    return value


def _https(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


def _candidate(
    *,
    provider: str,
    version: str | None,
    license_value: str | None,
    landing_url: str | None,
    pdf_url: str | None,
    source_record_url: str | None,
    match_evidence: dict[str, Any],
    rights_evidence: dict[str, Any],
) -> dict[str, Any]:
    exact_doi = match_evidence.get("doi_match") is True
    return {
        "provider": provider,
        "version": version or "unknown",
        "license": license_value or None,
        "landing_url": _https(landing_url),
        "pdf_url": _https(pdf_url),
        "source_record_url": _https(source_record_url),
        "match_evidence": match_evidence,
        "rights_evidence": rights_evidence,
        "rights_eligible": True,
        "confidence": "high" if exact_doi else "review",
        "direct_pdf_candidate": bool(pdf_url),
        "candidate_status": "candidate_not_verified",
    }


def _match_evidence(requested_doi: str, record_doi: str | None, expected_title: str, record_title: str) -> dict[str, Any]:
    normalized_record_doi = normalize_doi(record_doi)
    score = title_similarity(expected_title, record_title) if expected_title and record_title else None
    return {
        "method": "doi" if normalized_record_doi == requested_doi else "title",
        "requested_doi": requested_doi,
        "record_doi": normalized_record_doi,
        "doi_match": normalized_record_doi == requested_doi,
        "title_similarity": round(score, 4) if score is not None else None,
        "record_title": record_title or None,
    }


async def discover_unpaywall(
    transport: CandidateOnlyHTTP, doi: str, title: str, email: str
) -> list[dict[str, Any]]:
    if not email:
        raise CandidateDiscoveryError("Unpaywall requires --email or a saved local Unpaywall email")
    payload = await transport.get(
        "unpaywall",
        f"https://api.unpaywall.org/v2/{quote(doi, safe='')}",
        params={"email": email},
        accept="application/json",
    )
    data = _json_payload(payload, "unpaywall")
    if not data:
        return []
    evidence = _match_evidence(doi, data.get("doi"), title, str(data.get("title") or ""))
    if not evidence["doi_match"]:
        raise CandidateDiscoveryError("Unpaywall record DOI did not match the requested DOI")
    best = data.get("best_oa_location") or {}
    candidates = []
    for location in data.get("oa_locations") or []:
        if not isinstance(location, dict):
            continue
        pdf_url = location.get("url_for_pdf")
        landing_url = location.get("url_for_landing_page") or location.get("url")
        if not pdf_url and not landing_url:
            continue
        candidates.append(
            _candidate(
                provider="unpaywall",
                version=location.get("version"),
                license_value=location.get("license"),
                landing_url=landing_url,
                pdf_url=pdf_url,
                source_record_url=f"https://doi.org/{doi}",
                match_evidence=evidence,
                rights_evidence={
                    "basis": "Unpaywall OA location",
                    "host_type": location.get("host_type"),
                    "is_best": bool(best and location == best),
                    "evidence": location.get("evidence"),
                },
            )
        )
    return candidates


async def discover_openalex(
    transport: CandidateOnlyHTTP,
    doi: str,
    title: str,
    *,
    email: str,
    api_key: str,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if email:
        params["mailto"] = email
    if api_key:
        params["api_key"] = api_key
    payload = await transport.get(
        "openalex",
        f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='/')}",
        params=params,
        accept="application/json",
    )
    data = _json_payload(payload, "openalex")
    if not data:
        return []
    record_title = str(data.get("title") or data.get("display_name") or "")
    evidence = _match_evidence(doi, data.get("doi"), title, record_title)
    if not evidence["doi_match"]:
        raise CandidateDiscoveryError("OpenAlex record DOI did not match the requested DOI")
    oa_status = (data.get("open_access") or {}).get("oa_status")
    candidates = []
    for location in data.get("locations") or []:
        if not isinstance(location, dict) or location.get("is_oa") is not True:
            continue
        pdf_url = location.get("pdf_url")
        landing_url = location.get("landing_page_url")
        if not pdf_url and not landing_url:
            continue
        source = location.get("source") or {}
        candidates.append(
            _candidate(
                provider="openalex",
                version=location.get("version"),
                license_value=location.get("license") or location.get("license_id"),
                landing_url=landing_url,
                pdf_url=pdf_url,
                source_record_url=data.get("id"),
                match_evidence=evidence,
                rights_evidence={
                    "basis": "OpenAlex OA location",
                    "location_is_oa": bool(location.get("is_oa")),
                    "oa_status": oa_status,
                    "source_type": source.get("type"),
                    "source_name": source.get("display_name"),
                },
            )
        )
    return candidates


def _core_record_doi(record: dict[str, Any]) -> str | None:
    direct = normalize_doi(record.get("doi"))
    if direct:
        return direct
    for identifier in record.get("identifiers") or []:
        if isinstance(identifier, dict) and str(identifier.get("type", "")).casefold() == "doi":
            found = normalize_doi(identifier.get("identifier"))
            if found:
                return found
    return None


async def discover_core(
    transport: CandidateOnlyHTTP, doi: str, title: str, *, api_key: str
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "q": f'doi:"{doi}"',
        "limit": 5,
        # CORE work records may otherwise include the full text in the metadata
        # response for authenticated accounts. This benchmark needs links only.
        "exclude": "fullText,abstract",
    }
    if api_key:
        params["apiKey"] = api_key
    payload = await transport.get(
        "core",
        "https://api.core.ac.uk/v3/search/works/",
        params=params,
        accept="application/json",
    )
    data = _json_payload(payload, "core")
    candidates: list[dict[str, Any]] = []
    for record in data.get("results") or []:
        if not isinstance(record, dict):
            continue
        record_title = str(record.get("title") or "")
        record_doi = _core_record_doi(record)
        evidence = _match_evidence(doi, record_doi, title, record_title)
        if not evidence["doi_match"]:
            continue
        links = record.get("links") or []
        display_url = next(
            (
                item.get("url")
                for item in links
                if isinstance(item, dict) and item.get("type") == "display" and item.get("url")
            ),
            f"https://core.ac.uk/works/{record.get('id')}",
        )
        providers = [
            item.get("name")
            for item in record.get("dataProviders") or []
            if isinstance(item, dict) and item.get("name")
        ]
        rights = {
            "basis": "CORE open-repository index record",
            "data_providers": providers,
        }
        download_url = str(record.get("downloadUrl") or "").strip()
        if download_url:
            candidates.append(
                _candidate(
                    provider="core",
                    version="unknown",
                    license_value=record.get("license"),
                    landing_url=display_url,
                    pdf_url=download_url,
                    source_record_url=display_url,
                    match_evidence=evidence,
                    rights_evidence=rights,
                )
            )
        for fulltext_url in record.get("sourceFulltextUrls") or []:
            if not isinstance(fulltext_url, str) or not fulltext_url.strip():
                continue
            url = fulltext_url.strip()
            looks_pdf = ".pdf" in urlparse(url).path.casefold()
            candidates.append(
                _candidate(
                    provider="core",
                    version="unknown",
                    license_value=record.get("license"),
                    landing_url=display_url if looks_pdf else url,
                    pdf_url=url if looks_pdf else None,
                    source_record_url=display_url,
                    match_evidence=evidence,
                    rights_evidence=rights,
                )
            )
    return candidates


async def discover_europe_pmc(
    transport: CandidateOnlyHTTP, doi: str, title: str, *, email: str
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "query": f"DOI:{doi}",
        "format": "json",
        "resultType": "core",
        "pageSize": 5,
    }
    if email:
        params["email"] = email
    payload = await transport.get(
        "europe_pmc",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        params=params,
        accept="application/json",
    )
    data = _json_payload(payload, "europe_pmc")
    candidates: list[dict[str, Any]] = []
    for record in (data.get("resultList") or {}).get("result") or []:
        if not isinstance(record, dict):
            continue
        evidence = _match_evidence(
            doi,
            record.get("doi"),
            title,
            str(record.get("title") or ""),
        )
        if not evidence["doi_match"]:
            continue
        urls = (record.get("fullTextUrlList") or {}).get("fullTextUrl") or []
        oa_urls = [
            item
            for item in urls
            if isinstance(item, dict)
            and str(item.get("availabilityCode", "")).upper() == "OA"
            and item.get("url")
        ]
        landing = next(
            (
                item.get("url")
                for item in oa_urls
                if str(item.get("documentStyle", "")).casefold() == "html"
            ),
            None,
        )
        source = str(record.get("source") or "MED")
        identifier = str(record.get("id") or record.get("pmcid") or "")
        source_record = f"https://europepmc.org/article/{source}/{identifier}" if identifier else landing
        version = "acceptedVersion" if record.get("authMan") == "Y" else "publishedVersion"
        for item in oa_urls:
            style = str(item.get("documentStyle", "")).casefold()
            url = str(item.get("url"))
            if style not in {"pdf", "html"}:
                continue
            candidates.append(
                _candidate(
                    provider="europe_pmc",
                    version=version,
                    license_value=record.get("license"),
                    landing_url=landing or (url if style == "html" else source_record),
                    pdf_url=url if style == "pdf" else None,
                    source_record_url=source_record,
                    match_evidence=evidence,
                    rights_evidence={
                        "basis": "Europe PMC full-text URL",
                        "availability": item.get("availability"),
                        "availability_code": item.get("availabilityCode"),
                        "site": item.get("site"),
                        "is_open_access": record.get("isOpenAccess") == "Y",
                    },
                )
            )
    return candidates


async def discover_arxiv_batches(
    transport: CandidateOnlyHTTP,
    papers: list[tuple[str, str]],
    *,
    batch_size: int = 5,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Search several exact titles per arXiv request.

    The official API asks clients to wait three seconds between consecutive
    requests. CandidateOnlyHTTP enforces 3.1 seconds, and batching keeps this
    20-paper benchmark to at most four arXiv requests.
    """

    results: dict[str, list[dict[str, Any]]] = {doi: [] for doi, _title in papers}
    errors: dict[str, str] = {}
    searchable = [(doi, title) for doi, title in papers if title]
    for start in range(0, len(searchable), max(1, batch_size)):
        chunk = searchable[start : start + max(1, batch_size)]
        clauses = []
        for doi, title in chunk:
            escaped = " ".join(title.replace('"', " ").split())
            prefix, separator, suffix = doi.partition("/")
            query_doi = f"{prefix}/{suffix.upper()}" if separator and prefix == "10.1109" else doi
            clauses.append(f'doi:"{query_doi}"')
            clauses.append(f'ti:"{escaped}"')
        try:
            payload = await transport.get(
                "arxiv",
                "https://export.arxiv.org/api/query",
                params={
                    "search_query": " OR ".join(clauses),
                    "start": 0,
                    "max_results": max(10, len(chunk) * 3),
                },
                accept="application/atom+xml",
            )
            if payload.status_code == 404:
                continue
            root = ET.fromstring(payload.text)
        except (CandidateDiscoveryError, httpx.HTTPError, OSError, ET.ParseError) as exc:
            message = "arXiv returned invalid Atom XML" if isinstance(exc, ET.ParseError) else str(exc)
            for doi, _title in chunk:
                errors[doi] = message
            continue

        atom = "{http://www.w3.org/2005/Atom}"
        arxiv = "{http://arxiv.org/schemas/atom}"
        for entry in root.findall(f"{atom}entry"):
            record_title = " ".join((entry.findtext(f"{atom}title") or "").split())
            record_doi = normalize_doi(entry.findtext(f"{arxiv}doi"))
            target: tuple[str, str] | None = None
            if record_doi:
                target = next((paper for paper in chunk if paper[0] == record_doi), None)
            if target is None:
                scored = sorted(
                    ((title_similarity(title, record_title), doi, title) for doi, title in chunk),
                    reverse=True,
                )
                if scored and scored[0][0] >= 0.95:
                    target = (scored[0][1], scored[0][2])
            if target is None:
                continue
            doi, title = target
            evidence = _match_evidence(doi, record_doi, title, record_title)
            if record_doi and not evidence["doi_match"]:
                continue
            landing = entry.findtext(f"{atom}id")
            pdf_url = None
            for link in entry.findall(f"{atom}link"):
                if link.attrib.get("type") == "application/pdf" or link.attrib.get("title") == "pdf":
                    pdf_url = link.attrib.get("href")
                    break
            license_node = entry.find(f"{arxiv}license")
            license_value = license_node.attrib.get("href") if license_node is not None else None
            results[doi].append(
                _candidate(
                    provider="arxiv",
                    version="submittedVersion",
                    license_value=license_value,
                    landing_url=landing,
                    pdf_url=pdf_url,
                    source_record_url=landing,
                    match_evidence=evidence,
                    rights_evidence={
                        "basis": "arXiv repository record",
                        "license_url": license_value,
                    },
                )
            )
    return results, errors


def _deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        url = candidate.get("pdf_url") or candidate.get("landing_url") or candidate.get("source_record_url")
        key = (str(candidate.get("provider")), str(url or ""))
        if url and key not in seen:
            seen.add(key)
            deduplicated.append(candidate)
    return deduplicated


def load_baseline(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    records: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for item in data.get("papers") or []:
        doi = normalize_doi(item.get("doi"))
        if not doi:
            continue
        attachments = item.get("pdf_attachments") or []
        has_pdf = str(item.get("pdf_status", "")).casefold() in {"verified", "available", "complete"} or bool(
            attachments
        )
        records[doi] = {
            "has_pdf": has_pdf,
            "pdf_status": item.get("pdf_status"),
            "title": str(item.get("title") or ""),
        }
    stated = data.get("network_pdf_count")
    computed = sum(1 for item in records.values() if item["has_pdf"])
    if stated is not None and int(stated) != computed:
        warnings.append(f"baseline network_pdf_count={stated}, but {computed} records contain PDFs")
    return records, warnings


async def run_benchmark(
    *,
    input_path: Path,
    baseline_path: Path,
    output_path: Path,
    cache_dir: Path,
    email: str,
    openalex_api_key: str = "",
    core_api_key: str = "",
    transport: CandidateOnlyHTTP | None = None,
) -> dict[str, Any]:
    items = parse_input(Path(input_path).read_text(encoding="utf-8-sig"))
    baseline, warnings = load_baseline(baseline_path)
    if not items:
        raise ValueError("Input contains no papers")
    prepared_items: list[dict[str, Any]] = []
    for position, item in enumerate(items, start=1):
        doi = normalize_doi(item.get("doi"))
        baseline_item = baseline.get(doi or "", {})
        prepared_items.append(
            {
                "position": position,
                "doi": doi,
                "title": str(item.get("title") or baseline_item.get("title") or ""),
                "baseline": baseline_item,
            }
        )
    own_transport = transport is None
    client = transport or CandidateOnlyHTTP(
        cache_dir,
        user_agent=f"auto-paper-collector/0.5 candidate-only benchmark (mailto:{email})",
    )
    papers = []
    provider_failure_streak: Counter[str] = Counter()
    provider_circuit: dict[str, str] = {}
    try:
        arxiv_candidates, arxiv_errors = await discover_arxiv_batches(
            client,
            [
                (str(item["doi"]), str(item["title"]))
                for item in prepared_items
                if item["doi"]
            ],
        )
        for item in prepared_items:
            position = int(item["position"])
            doi = item["doi"]
            if not doi:
                papers.append(
                    {
                        "position": position,
                        "doi": None,
                        "title": item.get("title") or None,
                        "baseline_has_pdf": False,
                        "artifact_status": "candidate_not_verified",
                        "candidates": [],
                        "unique_pdf_candidate_urls": [],
                        "incremental_hit": False,
                        "errors": {"input": "A DOI is required for this benchmark"},
                    }
                )
                continue
            baseline_item = item["baseline"]
            title = str(item["title"])
            candidates: list[dict[str, Any]] = list(arxiv_candidates.get(doi, []))
            errors: dict[str, str] = {}
            if doi in arxiv_errors:
                errors["arxiv"] = arxiv_errors[doi]
            providers: list[tuple[str, Callable[[], Awaitable[list[dict[str, Any]]]]]] = [
                ("unpaywall", lambda doi=doi, title=title: discover_unpaywall(client, doi, title, email)),
                (
                    "openalex",
                    lambda doi=doi, title=title: discover_openalex(
                        client, doi, title, email=email, api_key=openalex_api_key
                    ),
                ),
                (
                    "core",
                    lambda doi=doi, title=title: discover_core(
                        client, doi, title, api_key=core_api_key
                    ),
                ),
                (
                    "europe_pmc",
                    lambda doi=doi, title=title: discover_europe_pmc(
                        client, doi, title, email=email
                    ),
                ),
            ]
            for provider, operation in providers:
                if provider in provider_circuit:
                    errors[provider] = (
                        "provider circuit open after 3 consecutive failures: "
                        f"{provider_circuit[provider]}"
                    )
                    continue
                try:
                    candidates.extend(await operation())
                    provider_failure_streak[provider] = 0
                except (CandidateDiscoveryError, httpx.HTTPError, OSError) as exc:
                    errors[provider] = str(exc)
                    provider_failure_streak[provider] += 1
                    if provider_failure_streak[provider] >= 3:
                        provider_circuit[provider] = str(exc)
            candidates = _deduplicate_candidates(candidates)
            pdf_urls = sorted(
                {
                    str(candidate["pdf_url"])
                    for candidate in candidates
                    if candidate.get("pdf_url")
                    and candidate.get("confidence") == "high"
                    and candidate.get("rights_eligible") is True
                }
            )
            baseline_has_pdf = bool(baseline_item.get("has_pdf"))
            papers.append(
                {
                    "position": position,
                    "doi": doi,
                    "title": title or None,
                    "baseline_has_pdf": baseline_has_pdf,
                    "baseline_pdf_status": baseline_item.get("pdf_status"),
                    "artifact_status": "verified_download" if baseline_has_pdf else "candidate_not_verified",
                    "candidates": candidates,
                    "unique_pdf_candidate_urls": pdf_urls,
                    "high_confidence_pdf_candidate": bool(pdf_urls),
                    "incremental_hit": bool(pdf_urls) and not baseline_has_pdf,
                    "errors": errors,
                }
            )
    finally:
        if own_transport:
            await client.close()

    baseline_pdf_count = sum(1 for paper in papers if paper["baseline_has_pdf"])
    incremental = [paper for paper in papers if paper["incremental_hit"]]
    incremental_by_provider: Counter[str] = Counter()
    for paper in incremental:
        providers = {
            candidate["provider"]
            for candidate in paper["candidates"]
            if candidate.get("pdf_url")
            and candidate.get("confidence") == "high"
            and candidate.get("rights_eligible") is True
        }
        incremental_by_provider.update(providers)
    errors_by_provider: Counter[str] = Counter()
    for paper in papers:
        errors_by_provider.update(paper["errors"].keys())

    def provider_error_counts(provider: str) -> tuple[int, int]:
        messages = [paper["errors"].get(provider, "") for paper in papers if provider in paper["errors"]]
        circuit_skips = sum(message.startswith("provider circuit open") for message in messages)
        return len(messages) - circuit_skips, circuit_skips

    transport_stats = client.serializable_stats()
    provider_status = {
        "unpaywall": {
            "configured": bool(email),
            "metadata_requests": transport_stats["network_requests_by_provider"].get("unpaywall", 0),
            "cache_hits": transport_stats["cache_hits_by_provider"].get("unpaywall", 0),
            "paper_errors": errors_by_provider.get("unpaywall", 0),
            "circuit_open": "unpaywall" in provider_circuit,
        },
        "openalex": {
            "api_key_configured": bool(openalex_api_key),
            "access_mode": "api_key" if openalex_api_key else "anonymous",
            "metadata_requests": transport_stats["network_requests_by_provider"].get("openalex", 0),
            "cache_hits": transport_stats["cache_hits_by_provider"].get("openalex", 0),
            "paper_errors": errors_by_provider.get("openalex", 0),
            "circuit_open": "openalex" in provider_circuit,
        },
        "core": {
            "api_key_configured": bool(core_api_key),
            "access_mode": "api_key" if core_api_key else "anonymous_rate_limited",
            "minimum_interval_seconds": client.intervals.get("core", 0.0),
            "metadata_requests": transport_stats["network_requests_by_provider"].get("core", 0),
            "cache_hits": transport_stats["cache_hits_by_provider"].get("core", 0),
            "paper_errors": errors_by_provider.get("core", 0),
            "circuit_open": "core" in provider_circuit,
            "circuit_reason": provider_circuit.get("core"),
        },
        "europe_pmc": {
            "metadata_requests": transport_stats["network_requests_by_provider"].get("europe_pmc", 0),
            "cache_hits": transport_stats["cache_hits_by_provider"].get("europe_pmc", 0),
            "paper_errors": errors_by_provider.get("europe_pmc", 0),
            "circuit_open": "europe_pmc" in provider_circuit,
        },
        "arxiv": {
            "batched": True,
            "batch_size": 5,
            "minimum_interval_seconds": client.intervals.get("arxiv", 0.0),
            "metadata_requests": transport_stats["network_requests_by_provider"].get("arxiv", 0),
            "cache_hits": transport_stats["cache_hits_by_provider"].get("arxiv", 0),
            "paper_errors": errors_by_provider.get("arxiv", 0),
        },
    }
    for provider, status in provider_status.items():
        request_failures, circuit_skips = provider_error_counts(provider)
        status["request_failures"] = request_failures
        status["circuit_skipped_papers"] = circuit_skips

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "candidate_discovery_only",
        "safety": {
            "pdf_downloads_attempted": 0,
            "candidate_files_verified": 0,
            "candidate_url_requests": transport_stats["candidate_url_requests"],
            "allowed_metadata_hosts": sorted(ALLOWED_METADATA_HOSTS),
        },
        "input": {
            "fixture": str(input_path),
            "baseline_report": str(baseline_path),
            "paper_count": len(papers),
        },
        "summary": {
            "paper_count": len(papers),
            "baseline_pdf_count": baseline_pdf_count,
            "baseline_missing_count": len(papers) - baseline_pdf_count,
            "verified_download_count": baseline_pdf_count,
            "candidate_not_verified_count": len(papers) - baseline_pdf_count,
            "papers_with_high_confidence_pdf_candidate": sum(
                1 for paper in papers if paper.get("high_confidence_pdf_candidate")
            ),
            "unique_incremental_hit_count": len(incremental),
            "unique_incremental_dois": [paper["doi"] for paper in incremental],
            "incremental_hits_by_provider": dict(sorted(incremental_by_provider.items())),
            "errors_by_provider": dict(sorted(errors_by_provider.items())),
            "measurement_complete": not bool(errors_by_provider),
            "incomplete_providers": sorted(errors_by_provider),
        },
        "provider_status": provider_status,
        "transport": transport_stats,
        "warnings": warnings,
        "papers": papers,
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _saved_email(settings: Settings) -> str:
    if settings.unpaywall_email:
        return settings.unpaywall_email
    if not settings.database_path.is_file():
        return ""
    try:
        uri = f"file:{settings.database_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key='unpaywall_email'"
            ).fetchone()
        return str(row[0]).strip() if row else ""
    except sqlite3.Error:
        return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover lawful OA candidates without requesting or downloading candidate files."
    )
    parser.add_argument("--input", type=Path, default=Path("examples/test_batch_20.csv"))
    parser.add_argument(
        "--baseline-report",
        type=Path,
        default=Path("outputs/test-batch-20-network-acceptance.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/test-batch-20-oa-candidates.json"),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("runtime/oa-candidate-cache"),
    )
    parser.add_argument("--email", default="", help="Contact email; defaults to the saved Unpaywall email.")
    parser.add_argument("--openalex-api-key", default=os.environ.get("OPENALEX_API_KEY", ""))
    parser.add_argument("--core-api-key", default=os.environ.get("CORE_API_KEY", ""))
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    settings = Settings.load()
    email = str(args.email or _saved_email(settings)).strip()
    report = await run_benchmark(
        input_path=args.input,
        baseline_path=args.baseline_report,
        output_path=args.output,
        cache_dir=args.cache_dir,
        email=email,
        openalex_api_key=str(args.openalex_api_key or "").strip(),
        core_api_key=str(args.core_api_key or "").strip(),
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Report: {Path(args.output).resolve()}")
    return 0


def main() -> int:
    return asyncio.run(_async_main(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
