"""Build a reproducible random acceptance-test sample from EndNote PDF storage.

The source library is never modified. Duplicate attachment bytes are collapsed,
the remaining PDFs are shuffled with a recorded seed, and only main articles
whose DOI and Crossref title can both be confirmed from the opening pages are
eligible. The detailed manifest is intended for ``outputs/`` (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_endnote.clients import normalize_crossref_work  # noqa: E402
from paper_endnote.inputs import DOI_RE, normalize_doi, normalize_title, title_similarity  # noqa: E402
from paper_endnote.pdf_validation import SUPPLEMENT_MARKERS, sha256_file  # noqa: E402


def opening_text(path: Path, page_limit: int = 3) -> tuple[str, int]:
    reader = PdfReader(str(path))
    parts: list[str] = []
    for page in reader.pages[:page_limit]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts), len(reader.pages)


def ordered_dois(text: str) -> list[str]:
    found: list[str] = []
    for match in DOI_RE.finditer(text[:18000]):
        doi = normalize_doi(match.group(0))
        if doi and doi not in found:
            found.append(doi)
    return found[:8]


def title_score(title: str, text: str) -> float:
    expected = normalize_title(title)
    opening = normalize_title(text[:12000])
    if expected and expected in opening:
        return 1.0
    lines = [line.strip() for line in text.splitlines()[:100] if line.strip()]
    candidates: list[str] = list(lines)
    for width in (2, 3, 4):
        candidates.extend(" ".join(lines[index:index + width]) for index in range(len(lines) - width + 1))
    return max((title_similarity(title, value) for value in candidates), default=0.0)


def crossref_by_doi(client: httpx.Client, doi: str) -> dict[str, Any] | None:
    response = client.get(f"https://api.crossref.org/works/{quote(doi, safe='')}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return normalize_crossref_work(response.json()["message"])


def identify_pdf(client: httpx.Client, path: Path, digest: str) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                return None
        text, page_count = opening_text(path)
    except Exception:
        return None
    compact = " ".join(text.split())
    marker_text = f"{path.name} {compact[:4000]}".casefold()
    if not compact or any(marker in marker_text for marker in SUPPLEMENT_MARKERS):
        return None

    candidates: list[dict[str, Any]] = []
    for doi_index, doi in enumerate(ordered_dois(text)):
        try:
            metadata = crossref_by_doi(client, doi)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            continue
        if not metadata or not metadata.get("title"):
            continue
        score = title_score(metadata["title"], text)
        candidates.append({"metadata": metadata, "title_score": score, "doi_index": doi_index})

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item["title_score"], -item["doi_index"]), reverse=True)
    best = candidates[0]
    if best["title_score"] < 0.68:
        return None
    metadata = best["metadata"]
    return {
        "source_pdf": str(path.resolve()),
        "sha256": digest,
        "page_count": page_count,
        "doi": metadata["doi"],
        "title": metadata["title"],
        "year": metadata.get("year"),
        "author": (metadata.get("authors") or [""])[0],
        "authors": metadata.get("authors") or [],
        "journal": metadata.get("journal") or "",
        "title_score": round(float(best["title_score"]), 4),
        "doi_candidate_position": int(best["doi_index"]) + 1,
    }


def unique_pdfs(pdf_root: Path) -> tuple[list[tuple[Path, str]], int]:
    unique: list[tuple[Path, str]] = []
    seen: set[str] = set()
    duplicates = 0
    for path in sorted(pdf_root.rglob("*.pdf")):
        digest = sha256_file(path)
        if digest in seen:
            duplicates += 1
            continue
        seen.add(digest)
        unique.append((path, digest))
    return unique, duplicates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-root", type=Path, required=True)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--output", type=Path, default=Path("outputs/endnote-random-sample.json"))
    parser.add_argument("--mailto", default="")
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be positive")
    pdf_root = args.pdf_root.resolve()
    if not pdf_root.is_dir():
        parser.error(f"PDF root does not exist: {pdf_root}")

    seed = args.seed if args.seed is not None else secrets.randbits(64)
    corpus, duplicate_count = unique_pdfs(pdf_root)
    random.Random(seed).shuffle(corpus)
    headers = {"User-Agent": f"PaperEndNote/0.5 random acceptance sampler; mailto:{args.mailto}"}
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    selected_dois: set[str] = set()
    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True, trust_env=False) as client:
        for path, digest in corpus:
            relative_source = path.relative_to(pdf_root).as_posix()
            identified = identify_pdf(client, path, digest)
            if not identified:
                rejected.append({"source_pdf": relative_source, "reason": "identity_not_confirmed"})
                continue
            doi = identified["doi"]
            if doi in selected_dois:
                rejected.append({"source_pdf": relative_source, "reason": "duplicate_doi"})
                continue
            selected_dois.add(doi)
            identified["source_pdf"] = relative_source
            identified["position"] = len(selected) + 1
            selected.append(identified)
            if len(selected) == args.count:
                break

    result = {
        "schema_version": 1,
        "seed": seed,
        "requested_count": args.count,
        "selected_count": len(selected),
        "source_pdf_count": len(list(pdf_root.rglob("*.pdf"))),
        "unique_pdf_count": len(corpus),
        "duplicate_file_count": duplicate_count,
        "selection_rule": "unique SHA-256; shuffled by seed; Crossref DOI and title confirmed in opening pages; main PDF",
        "selected": selected,
        "rejected_before_completion": rejected,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if len(selected) == args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
