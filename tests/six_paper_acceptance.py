from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from paper_endnote.clients import CrossrefClient
from paper_endnote.config import Settings
from paper_endnote.downloader import download_pdf
from paper_endnote.inputs import parse_input, title_similarity
from paper_endnote.pdf_validation import validate_pdf


PUBLIC_PDF_CANDIDATES = {
    "10.1109/surv.2012.042512.00053": (
        "https://citeseerx.ist.psu.edu/document?doi=fad19ff585577ad33275d447743a91c91e18704b&rep1&type=pdf",
        "accepted-or-prepublication",
    ),
    "10.1016/j.jnca.2016.01.002": (
        "https://www.cs.unibo.it/~marfia/pubblicazioni/j023.pdf",
        "accepted",
    ),
    "10.1109/surv.2012.022412.00068": (
        "https://lpdwww.epfl.ch/pdutta/schedulingSurvey.pdf",
        "accepted-or-prepublication",
    ),
    "10.1109/surv.2013.011413.00082": (
        "https://dspace.networks.imdea.org/bitstream/handle/20.500.12761/1100/oppsurvey.pdf?isAllowed=y&sequence=1",
        "accepted-or-prepublication",
    ),
    "10.1109/comst.2021.3058573": (
        "https://oulurepo.oulu.fi/bitstream/handle/10024/30908/nbnfi-fe2021090144887.pdf?sequence=1",
        "repository-copy",
    ),
    "10.1109/access.2024.3446569": (
        "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=10643106",
        "published",
    ),
    "10.1109/jiot.2026.3712402": (
        "https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber=11622448",
        "published-or-early-access",
    ),
}


async def run(source_csv: Path, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PAPER_ENDNOTE_DATA"] = str(output_dir / "runtime")
    settings = Settings.load()
    client = CrossrefClient(settings)
    papers = parse_input(source_csv.read_text(encoding="utf-8-sig"))
    results: list[dict[str, Any]] = []
    try:
        for position, paper in enumerate(papers, start=1):
            doi = paper["doi"]
            row: dict[str, Any] = {
                "position": position,
                "input_doi": doi,
                "input_title": paper.get("title"),
                "metadata": "failed",
                "pdf": "not_tested",
            }
            try:
                metadata = await client.by_doi(doi)
                similarity = title_similarity(paper.get("title"), metadata.get("title"))
                row.update(
                    metadata="matched" if similarity >= 0.85 else "title_mismatch",
                    metadata_title=metadata.get("title"),
                    metadata_year=metadata.get("year"),
                    title_similarity=round(similarity, 4),
                )
            except Exception as exc:
                row["metadata_error"] = str(exc)

            candidate = PUBLIC_PDF_CANDIDATES.get(doi)
            if candidate:
                url, expected_version = candidate
                destination = output_dir / "pdf" / f"{position:02d}.pdf"
                try:
                    await download_pdf(url, destination, settings)
                    validation = validate_pdf(
                        destination,
                        expected_doi=doi,
                        expected_title=row.get("metadata_title") or paper.get("title"),
                    )
                    row.update(
                        pdf=validation.identity if validation.valid_pdf else "invalid",
                        pdf_identity=validation.identity,
                        pdf_role=validation.role,
                        pdf_reason=validation.reason,
                        pdf_sha256=validation.sha256,
                        pdf_bytes=destination.stat().st_size,
                        source_url=url,
                        expected_version=expected_version,
                    )
                except Exception as exc:
                    row.update(pdf="download_failed", pdf_error=str(exc), source_url=url)
            else:
                row.update(pdf="manual_mcgill_required", expected_version="published")
            results.append(row)
    finally:
        await client.close()

    summary = {
        "paper_count": len(results),
        "metadata_matched": sum(item["metadata"] == "matched" for item in results),
        "pdf_verified": sum(item["pdf"] == "verified" for item in results),
        "pdf_needs_review": sum(item["pdf"] == "needs_review" for item in results),
        "pdf_rejected": sum(item["pdf"] == "rejected" for item in results),
        "pdf_manual_required": sum(item["pdf"] == "manual_mcgill_required" for item in results),
        "pdf_download_failed": sum(item["pdf"] == "download_failed" for item in results),
        "results": results,
    }
    report = output_dir / "dtn-acceptance.json"
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the reproducible DTN paper network/PDF acceptance test")
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "examples/dtn_acceptance.csv")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "work/dtn-acceptance")
    args = parser.parse_args()
    result = asyncio.run(run(args.input.resolve(), args.output.resolve()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
