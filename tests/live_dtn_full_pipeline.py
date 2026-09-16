"""Create the eight-paper DTN batch and wait for OA + institution + Zotero commit.

Does not POST per-paper institution acquisition. Requires a running local app
and a Zotero instance. Complete McGill login/2FA in the dedicated Chrome window
if the batch pauses on a login page.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "examples" / "dtn_acceptance.csv"
OUTPUT = ROOT / "outputs" / "dtn-eight-paper-full-pipeline.json"
IN_FLIGHT = {"queued", "matching", "ready", "looking_for_pdf", "institution_pending"}


def wait_for(client: httpx.Client, batch_id: str, predicate, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/batches/{batch_id}")
        response.raise_for_status()
        last = response.json()
        if predicate(last):
            return last
        time.sleep(1.0)
    raise TimeoutError(f"Batch {batch_id} did not reach the expected state")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--collection", default="DTN")
    parser.add_argument("--library-mode", default="existing", choices=("existing", "new"))
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    fixture_text = args.fixture.read_text(encoding="utf-8-sig")
    with httpx.Client(
        base_url=args.base_url, timeout=60.0, follow_redirects=True, trust_env=False
    ) as client:
        client.get("/").raise_for_status()
        state = client.get("/api/state")
        state.raise_for_status()
        zotero = state.json()["zotero"]
        if not zotero.get("ready"):
            raise RuntimeError(f"Zotero is not ready: {zotero}")

        created = client.post(
            "/api/batches",
            json={
                "name": "DTN full pipeline",
                "items_text": fixture_text,
                "target_library": args.collection,
                "library_mode": args.library_mode,
                "reference_manager": "zotero",
                "start_immediately": True,
            },
        )
        created.raise_for_status()
        batch_id = created.json()["id"]
        print(json.dumps({"event": "batch_created", "batch_id": batch_id}, ensure_ascii=False), flush=True)
        batch = wait_for(
            client,
            batch_id,
            lambda value: all(paper["status"] not in IN_FLIGHT for paper in value["papers"])
            and all(paper["endnote_status"] in {"verified", "uncertain", "pending"} for paper in value["papers"])
            and value["status"] in {"completed", "failed"},
            timeout=900.0,
        )
        if any(paper["endnote_status"] == "pending" and paper["metadata_status"] == "verified" for paper in batch["papers"]):
            client.post(f"/api/batches/{batch_id}/commit").raise_for_status()
            batch = wait_for(
                client,
                batch_id,
                lambda value: all(
                    paper["endnote_status"] in {"verified", "uncertain"}
                    or paper["metadata_status"] != "verified"
                    for paper in value["papers"]
                ),
                timeout=300.0,
            )

    papers = [
        {
            "position": paper["position"],
            "doi": paper.get("doi"),
            "title": paper.get("title"),
            "status": paper.get("status"),
            "pdf_status": paper.get("pdf_status"),
            "record_number": paper.get("record_number"),
            "error": paper.get("error"),
        }
        for paper in batch["papers"]
    ]
    pdfs = [paper for paper in papers if paper["pdf_status"] in {"verified", "accepted"}]
    result = {
        "passed": len(pdfs) >= 7 and all(
            paper.get("endnote_status") == "verified" for paper in batch["papers"] if paper.get("metadata_status") == "verified"
        ),
        "batch_id": batch_id,
        "pdf_obtained_count": len(pdfs),
        "pdf_unresolved_count": len(papers) - len(pdfs),
        "papers": papers,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
