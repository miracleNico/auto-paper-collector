"""Destructive-in-the-small live acceptance test for a running Zotero instance.

The script talks only to the local Paper Reference Workflow web service.  It
imports the eight-paper DTN fixture twice and verifies that the second pass
reuses the same Zotero records and does not add duplicate PDF attachments.
Run it only against a Zotero library that the user has approved for testing.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx


PDF_BY_POSITION = {2: "02.pdf", 4: "04.pdf", 5: "05.pdf"}


def wait_for(client: httpx.Client, batch_id: str, predicate, *, timeout: float = 180.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/batches/{batch_id}")
        response.raise_for_status()
        last = response.json()
        if predicate(last):
            return last
        time.sleep(0.5)
    raise TimeoutError(f"Batch {batch_id} did not reach the expected state: {last}")


def prepare_batch(
    client: httpx.Client,
    *,
    fixture_text: str,
    collection: str,
    name: str,
    collection_mode: str,
    pdf_dir: Path,
) -> dict[str, Any]:
    response = client.post(
        "/api/batches",
        json={
            "name": name,
            "items_text": fixture_text,
            "target_library": collection,
            "library_mode": collection_mode,
            "reference_manager": "zotero",
            "start_immediately": True,
        },
    )
    response.raise_for_status()
    batch_id = response.json()["id"]
    batch = wait_for(
        client,
        batch_id,
        lambda value: all(
            paper["metadata_status"] in {"verified", "needs_review", "unavailable"}
                and paper["status"] not in {"queued", "matching", "ready", "looking_for_pdf", "institution_pending"}
            for paper in value["papers"]
        ),
    )
    bad_metadata = [paper for paper in batch["papers"] if paper["metadata_status"] != "verified"]
    if bad_metadata:
        raise RuntimeError(f"Metadata resolution failed: {bad_metadata}")

    for paper in batch["papers"]:
        filename = PDF_BY_POSITION.get(int(paper["position"]))
        if not filename:
            continue
        pdf_path = (pdf_dir / filename).resolve()
        if not pdf_path.is_file():
            raise FileNotFoundError(pdf_path)
        with pdf_path.open("rb") as handle:
            upload = client.post(
                f"/api/papers/{paper['id']}/upload-pdf",
                files={"file": (pdf_path.name, handle, "application/pdf")},
                timeout=120.0,
            )
        upload.raise_for_status()
        confirm = client.post(
            f"/api/papers/{paper['id']}/confirm-pdf",
            json={"accept": True, "version": "accepted"},
        )
        confirm.raise_for_status()

    commit = client.post(f"/api/batches/{batch_id}/commit")
    commit.raise_for_status()
    return wait_for(
        client,
        batch_id,
        lambda value: all(paper["endnote_status"] in {"verified", "uncertain"} for paper in value["papers"]),
        timeout=240.0,
    )


def summarize(batch: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_id": batch["id"],
        "status": batch["status"],
        "records": {str(paper["position"]): paper["record_number"] for paper in batch["papers"]},
        "papers": [
            {
                "position": paper["position"],
                "doi": paper["doi"],
                "title": paper["title"],
                "pdf_status": paper["pdf_status"],
                "reference_status": paper["endnote_status"],
                "record_key": paper["record_number"],
                "error": paper["error"],
            }
            for paper in batch["papers"]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--collection", default="DTN")
    parser.add_argument("--fixture", type=Path, default=Path("examples/dtn_acceptance.csv"))
    parser.add_argument("--pdf-dir", type=Path, default=Path("work/dtn-acceptance/pdf"))
    parser.add_argument("--output", type=Path, default=Path("outputs/zotero-live-acceptance.json"))
    args = parser.parse_args()

    fixture_text = args.fixture.resolve().read_text(encoding="utf-8-sig")
    with httpx.Client(base_url=args.base_url, timeout=60.0, follow_redirects=True, trust_env=False) as client:
        client.get("/").raise_for_status()  # establishes the local CSRF/session cookie
        state = client.get("/api/state")
        state.raise_for_status()
        zotero = state.json()["zotero"]
        if not zotero.get("ready"):
            raise RuntimeError(f"Zotero is not ready: {zotero}")

        first = prepare_batch(
            client,
            fixture_text=fixture_text,
            collection=args.collection,
            name="DTN Zotero live acceptance pass 1",
            collection_mode="new",
            pdf_dir=args.pdf_dir,
        )
        second = prepare_batch(
            client,
            fixture_text=fixture_text,
            collection=args.collection,
            name="DTN Zotero live acceptance pass 2",
            collection_mode="existing",
            pdf_dir=args.pdf_dir,
        )

    first_summary = summarize(first)
    second_summary = summarize(second)
    failures = [paper for paper in first_summary["papers"] + second_summary["papers"] if paper["reference_status"] != "verified"]
    same_keys = first_summary["records"] == second_summary["records"]
    result = {
        "passed": not failures and same_keys,
        "collection": args.collection,
        "paper_count": 8,
        "expected_pdf_count": len(PDF_BY_POSITION),
        "same_record_keys_on_repeat": same_keys,
        "failures": failures,
        "first_pass": first_summary,
        "second_pass": second_summary,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
