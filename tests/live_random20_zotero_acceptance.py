"""Run the reproducible 20-paper EndNote-to-Zotero closed-loop test.

This is a live, intentionally mutating test. It adds/reuses bibliographic
records in the approved Zotero collection and imports the sampled EndNote PDFs.
It runs the same batch twice and verifies record and PDF-attachment idempotence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from io import StringIO
from pathlib import Path
from typing import Any

import httpx


ACTIVE_STATUSES = {"queued", "matching", "ready", "looking_for_pdf", "institution_pending"}


def wait_for(client: httpx.Client, batch_id: str, predicate, *, timeout: float = 300.0) -> dict[str, Any]:
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


def settings_payload(state: dict[str, Any], *, test_mode: bool) -> dict[str, Any]:
    current = state["settings"]
    institution = dict(current["institution"])
    return {
        "crossref_email": current.get("crossref_email", ""),
        "unpaywall_email": current.get("unpaywall_email", ""),
        "endnote_library": current.get("endnote_library", ""),
        "acquisition_sources": ["open_access"] if test_mode else current.get("acquisition_sources", []),
        "ocr_enabled": current.get("ocr_enabled", True),
        "ocr_languages": current.get("ocr_languages", "eng"),
        "ocr_max_pages": current.get("ocr_max_pages", 2),
        "institution_preset": "",
        "institution": institution,
        "auto_institution": False if test_mode else current.get("auto_institution", True),
        "auto_commit": False if test_mode else current.get("auto_commit", True),
        "login_wait_seconds": current.get("login_wait_seconds", 600),
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_source_pdf(pdf_root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise RuntimeError("Sample manifest must contain paths relative to --pdf-root")
    candidate = (pdf_root / relative).resolve()
    try:
        candidate.relative_to(pdf_root)
    except ValueError as exc:
        raise RuntimeError(f"Sample PDF path escapes --pdf-root: {value}") from exc
    return candidate


def load_sample(
    manifest_path: Path, fixture_path: Path, pdf_root: Path
) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = manifest.get("selected") or []
    if len(selected) != 20:
        raise RuntimeError(f"Expected a 20-paper sample, got {len(selected)}")
    fixture_dois = [
        str(row.get("doi") or "").strip().casefold()
        for row in csv.DictReader(StringIO(fixture_path.read_text(encoding="utf-8-sig")))
        if str(row.get("doi") or "").strip()
    ]
    sample_dois = [str(item["doi"]).casefold() for item in selected]
    if fixture_dois != sample_dois:
        raise RuntimeError("Fixture DOI order does not match the sampled manifest")
    resolved: list[dict[str, Any]] = []
    for raw_item in selected:
        item = dict(raw_item)
        path = resolve_source_pdf(pdf_root, str(item["source_pdf"]))
        if not path.is_file():
            raise FileNotFoundError(path)
        if file_sha256(path) != item["sha256"]:
            raise RuntimeError(f"Source PDF changed since sampling: {path}")
        item["_source_pdf_path"] = path
        resolved.append(item)
    return resolved


def prepare_pass(
    client: httpx.Client,
    *,
    selected: list[dict[str, Any]],
    fixture_text: str,
    collection: str,
    name: str,
    collection_mode: str,
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
        lambda value: all(paper["status"] not in ACTIVE_STATUSES for paper in value["papers"]),
    )
    unresolved = [paper for paper in batch["papers"] if paper["metadata_status"] != "verified"]
    if unresolved:
        raise RuntimeError(f"Metadata resolution failed: {unresolved}")

    source_by_position = {
        int(item["position"]): Path(item["_source_pdf_path"]) for item in selected
    }
    for paper in batch["papers"]:
        if paper["pdf_status"] in {"verified", "accepted"}:
            continue
        pdf_path = source_by_position[int(paper["position"])]
        with pdf_path.open("rb") as handle:
            upload = client.post(
                f"/api/papers/{paper['id']}/upload-pdf",
                files={"file": (pdf_path.name, handle, "application/pdf")},
                timeout=180.0,
            )
        upload.raise_for_status()
        validation = upload.json()
        if validation.get("identity") != "verified" or validation.get("role") != "main":
            raise RuntimeError(f"PDF validation failed for position {paper['position']}: {validation}")

    commit = client.post(f"/api/batches/{batch_id}/commit")
    commit.raise_for_status()
    return wait_for(
        client,
        batch_id,
        lambda value: all(paper["endnote_status"] in {"verified", "uncertain"} for paper in value["papers"]),
        timeout=600.0,
    )


def pdf_children(client: httpx.Client, item_key: str) -> list[dict[str, Any]]:
    response = client.get(f"http://127.0.0.1:23119/api/users/0/items/{item_key}/children")
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
    parser.add_argument("--collection", default="test")
    parser.add_argument("--pdf-root", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=Path("examples/test_batch_20.csv"))
    parser.add_argument("--sample", type=Path, default=Path("outputs/test-batch-20-sample.json"))
    parser.add_argument("--output", type=Path, default=Path("outputs/test-batch-20-local-acceptance.json"))
    args = parser.parse_args()

    selected = load_sample(
        args.sample.resolve(), args.fixture.resolve(), args.pdf_root.resolve()
    )
    fixture_text = args.fixture.resolve().read_text(encoding="utf-8-sig")
    original_settings: dict[str, Any] | None = None
    with httpx.Client(base_url=args.base_url, timeout=60.0, follow_redirects=True, trust_env=False) as client:
        client.get("/").raise_for_status()
        state_response = client.get("/api/state")
        state_response.raise_for_status()
        state = state_response.json()
        if not state["zotero"].get("ready"):
            raise RuntimeError(f"Zotero is not ready: {state['zotero']}")
        original_settings = settings_payload(state, test_mode=False)
        changed = client.post("/api/settings", json=settings_payload(state, test_mode=True))
        changed.raise_for_status()
        try:
            first = prepare_pass(
                client,
                selected=selected,
                fixture_text=fixture_text,
                collection=args.collection,
                name="Test Batch 20",
                collection_mode="new",
            )
            first_attachments = {
                str(paper["position"]): pdf_children(client, paper["record_number"])
                for paper in first["papers"]
            }
            second = prepare_pass(
                client,
                selected=selected,
                fixture_text=fixture_text,
                collection=args.collection,
                name="Test Batch 20",
                collection_mode="existing",
            )
            second_attachments = {
                str(paper["position"]): pdf_children(client, paper["record_number"])
                for paper in second["papers"]
            }
        finally:
            if original_settings is not None:
                restored = client.post("/api/settings", json=original_settings)
                restored.raise_for_status()

    first_summary = summarize(first)
    second_summary = summarize(second)
    failures = [
        paper for paper in first_summary["papers"] + second_summary["papers"]
        if paper["reference_status"] != "verified"
    ]
    same_keys = first_summary["records"] == second_summary["records"]
    same_attachments = first_attachments == second_attachments
    all_have_pdf = all(first_attachments[str(index)] for index in range(1, 21))
    result = {
        "passed": not failures and same_keys and same_attachments and all_have_pdf,
        "collection": args.collection,
        "paper_count": 20,
        "sample_seed": json.loads(args.sample.resolve().read_text(encoding="utf-8"))["seed"],
        "same_record_keys_on_repeat": same_keys,
        "same_pdf_attachments_on_repeat": same_attachments,
        "all_records_have_pdf": all_have_pdf,
        "failures": failures,
        "first_pass": first_summary,
        "second_pass": second_summary,
        "first_pdf_attachments": first_attachments,
        "second_pdf_attachments": second_attachments,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
