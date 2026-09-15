from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Any


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    cleaned = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^doi\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    match = DOI_RE.search(cleaned)
    if not match:
        return None
    return match.group(0).rstrip(".,;])}").lower()


def normalize_title(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _int_year(value: Any) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"\b(18|19|20|21)\d{2}\b", str(value))
    return int(match.group(0)) if match else None


def _from_mapping(row: dict[str, Any]) -> dict[str, Any] | None:
    lowered = {str(key).strip().casefold(): value for key, value in row.items() if key is not None}
    doi = normalize_doi(str(lowered.get("doi", "")))
    title = str(lowered.get("title", "") or "").strip()
    author = str(lowered.get("author", lowered.get("authors", "")) or "").strip() or None
    year = _int_year(lowered.get("year"))
    if not doi and not title:
        return None
    input_text = doi or title
    return {"input_text": input_text, "doi": doi, "title": title or None, "year": year, "author": author}


def parse_input(text: str) -> list[dict[str, Any]]:
    text = text.replace("\ufeff", "").strip()
    if not text:
        return []

    lines = [line for line in text.splitlines() if line.strip()]
    first = lines[0].casefold()
    items: list[dict[str, Any]] = []
    if any(header in first for header in ("doi", "title", "author", "year")) and any(
        delimiter in lines[0] for delimiter in (",", "\t", ";")
    ):
        try:
            dialect = csv.Sniffer().sniff("\n".join(lines[:5]), delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        for row in csv.DictReader(io.StringIO("\n".join(lines)), dialect=dialect):
            parsed = _from_mapping(row)
            if parsed:
                items.append(parsed)
    else:
        for line in lines:
            value = line.strip()
            doi = normalize_doi(value)
            if doi:
                title = value.replace(doi, "", 1).strip(" \t,-—|;") or None
                items.append({"input_text": value, "doi": doi, "title": title, "year": None, "author": None})
            else:
                items.append({"input_text": value, "doi": None, "title": value, "year": None, "author": None})

    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = f"doi:{item['doi']}" if item.get("doi") else f"title:{normalize_title(item.get('title'))}:{item.get('year') or ''}"
        if key not in seen:
            seen.add(key)
            deduplicated.append(item)
    return deduplicated


def title_similarity(left: str | None, right: str | None) -> float:
    from difflib import SequenceMatcher

    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()
