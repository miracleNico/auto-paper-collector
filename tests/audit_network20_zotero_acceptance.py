"""Audit a completed network batch and run a download-free idempotence pass."""

from __future__ import annotations

import argparse
import csv
import json
import time
from io import StringIO
from pathlib import Path
from typing import Any

import httpx


ACTIVE = {"queued", "matching", "ready", "looking_for_pdf", "institution_pending"}


def wait_batch(client: httpx.Client, batch_id: str, predicate, timeout: float = 300) -> dict[str, Any]:
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


def settings_payload(state: dict[str, Any], *, audit_mode: bool) -> dict[str, Any]:
    current = state["settings"]
    return {
        "crossref_email": current.get("crossref_email", ""),
        "unpaywall_email": "" if audit_mode else current.get("unpaywall_email", ""),
        "endnote_library": current.get("endnote_library", ""),
        "acquisition_sources": ["open_access"] if audit_mode else current.get("acquisition_sources", []),
        "ocr_enabled": current.get("ocr_enabled", True),
        "ocr_languages": current.get("ocr_languages", "eng"),
        "ocr_max_pages": current.get("ocr_max_pages", 2),
        "institution_preset": "",
        "institution": current["institution"],
        "auto_institution": False if audit_mode else current.get("auto_institution", True),
        "auto_commit": False if audit_mode else current.get("auto_commit", True),
        "login_wait_seconds": current.get("login_wait_seconds", 600),
    }


def pdf_children(client: httpx.Client, item_key: str) -> list[dict[str, str]]:
    response = client.get(
        f"http://127.0.0.1:23119/api/users/0/items/{item_key}/children",
        headers={"Zotero-API-Version": "3"},
    )
    response.raise_for_status()
    result = []
    for item in response.json():
        data = item.get("data", item)
        if data.get("contentType") == "application/pdf":
            result.append({
                "key": data.get("key") or item.get("key"),
                "filename": data.get("filename") or "",
                "md5": data.get("md5") or "",
            })
    return sorted(result, key=lambda value: value["key"])


def records(batch: dict[str, Any]) -> dict[str, str | None]:
    return {str(paper["position"]): paper["record_number"] for paper in batch["papers"]}


def attachments(client: httpx.Client, batch: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    return {
        str(paper["position"]): pdf_children(client, paper["record_number"])
        for paper in batch["papers"]
        if paper.get("record_number")
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first-batch-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--collection", default="test")
    parser.add_argument("--fixture", type=Path, default=Path("examples/test_batch_20.csv"))
    parser.add_argument("--output", type=Path, default=Path("outputs/test-batch-20-network-acceptance.json"))
    args = parser.parse_args()

    fixture_text = args.fixture.resolve().read_text(encoding="utf-8-sig")
    fixture_rows = list(csv.DictReader(StringIO(fixture_text)))
    expected_dois = [str(row.get("doi") or "").casefold() for row in fixture_rows]
    if len(expected_dois) != 20:
        raise RuntimeError(f"Expected 20 fixture rows, got {len(expected_dois)}")

    original_settings: dict[str, Any] | None = None
    with httpx.Client(base_url=args.base_url, timeout=60, follow_redirects=True, trust_env=False) as client:
        client.get("/").raise_for_status()
        state = client.get("/api/state").json()
        if not state["zotero"].get("ready"):
            raise RuntimeError(f"Zotero is not ready: {state['zotero']}")
        original_settings = settings_payload(state, audit_mode=False)
        first = client.get(f"/api/batches/{args.first_batch_id}").json()
        first_dois = [str(paper.get("doi") or "").casefold() for paper in first["papers"]]
        if first_dois != expected_dois:
            raise RuntimeError("First batch does not match the canonical 20-paper fixture")
        if any(paper["endnote_status"] != "verified" for paper in first["papers"]):
            raise RuntimeError("First batch has unverified Zotero records")
        first_attachments = attachments(client, first)

        changed = client.post("/api/settings", json=settings_payload(state, audit_mode=True))
        changed.raise_for_status()
        try:
            response = client.post(
                "/api/batches",
                json={
                    "name": "Test Batch 20",
                    "items_text": fixture_text,
                    "target_library": args.collection,
                    "library_mode": "existing",
                    "reference_manager": "zotero",
                    "start_immediately": True,
                },
            )
            response.raise_for_status()
            second_id = response.json()["id"]
            second = wait_batch(
                client,
                second_id,
                lambda batch: all(paper["status"] not in ACTIVE for paper in batch["papers"]),
            )
            if any(paper["metadata_status"] != "verified" for paper in second["papers"]):
                raise RuntimeError("Second-pass metadata resolution failed")
            client.post(f"/api/batches/{second_id}/commit").raise_for_status()
            second = wait_batch(
                client,
                second_id,
                lambda batch: all(paper["endnote_status"] in {"verified", "uncertain"} for paper in batch["papers"]),
                timeout=600,
            )
            second_attachments = attachments(client, second)
        finally:
            if original_settings is not None:
                client.post("/api/settings", json=original_settings).raise_for_status()

    first_records = records(first)
    second_records = records(second)
    first_pdf_count = sum(bool(value) for value in first_attachments.values())
    result = {
        "passed": (
            first_records == second_records
            and first_attachments == second_attachments
            and all(paper["endnote_status"] == "verified" for paper in second["papers"])
        ),
        "collection": args.collection,
        "paper_count": 20,
        "network_pdf_count": first_pdf_count,
        "metadata_only_count": 20 - first_pdf_count,
        "same_record_keys_on_repeat": first_records == second_records,
        "same_pdf_attachments_on_repeat": first_attachments == second_attachments,
        "first_batch_id": first["id"],
        "second_batch_id": second["id"],
        "papers": [
            {
                "position": paper["position"],
                "doi": paper["doi"],
                "title": paper["title"],
                "pdf_status": paper["pdf_status"],
                "source_url": paper["source_url"],
                "zotero_record_key": paper["record_number"],
                "pdf_attachments": first_attachments[str(paper["position"])],
                "error": paper["error"],
            }
            for paper in first["papers"]
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
