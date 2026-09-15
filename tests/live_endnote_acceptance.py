"""Live acceptance test for an existing, manually created EndNote library.

The test imports the eight-paper DTN fixture twice through the local service.
One pre-write backup is shared by both passes because they form one acceptance
transaction.  The second pass must reuse record numbers and PDF attachments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

from paper_endnote.config import Settings
from paper_endnote.db import Database
from paper_endnote.endnote import (
    EndNoteAdapter,
    match_records,
    parse_endnote_xml,
    resolve_internal_attachment,
)


PDF_BY_POSITION = {2: "02.pdf", 4: "04.pdf", 5: "05.pdf"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_for(client: httpx.Client, batch_id: str, predicate, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/batches/{batch_id}")
        response.raise_for_status()
        last = response.json()
        if predicate(last):
            return last
        time.sleep(0.75)
    raise TimeoutError(f"Batch {batch_id} did not reach the expected state: {last}")


def create_prepared_batch(
    client: httpx.Client,
    *,
    fixture_text: str,
    library: Path,
    name: str,
    pdf_dir: Path,
) -> dict[str, Any]:
    response = client.post(
        "/api/batches",
        json={
            "name": name,
            "items_text": fixture_text,
            "target_library": str(library),
            "library_mode": "existing",
            "reference_manager": "endnote",
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
            and paper["status"] not in {"queued", "matching", "ready", "looking_for_pdf"}
            for paper in value["papers"]
        ),
        timeout=240.0,
    )
    bad_metadata = [paper for paper in batch["papers"] if paper["metadata_status"] != "verified"]
    if bad_metadata:
        raise RuntimeError(f"Metadata resolution failed: {bad_metadata}")

    for paper in batch["papers"]:
        filename = PDF_BY_POSITION.get(int(paper["position"]))
        if not filename:
            continue
        pdf_path = (pdf_dir / filename).resolve()
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
    return batch


def commit_and_wait(client: httpx.Client, batch_id: str) -> dict[str, Any]:
    response = client.post(f"/api/batches/{batch_id}/commit")
    response.raise_for_status()
    return wait_for(
        client,
        batch_id,
        lambda value: all(paper["endnote_status"] in {"verified", "uncertain"} for paper in value["papers"]),
        timeout=900.0,
    )


def metadata_for(paper: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(paper.get("metadata") or {})
    metadata.update(
        {
            "doi": paper.get("doi"),
            "title": paper.get("title"),
            "year": paper.get("year"),
            "authors": paper.get("authors") or [],
            "journal": paper.get("journal") or "",
        }
    )
    return metadata


def summarize(batch: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_id": batch["id"],
        "status": batch["status"],
        "backup_path": batch.get("backup_path"),
        "records": {str(paper["position"]): paper["record_number"] for paper in batch["papers"]},
        "failures": [
            {
                "position": paper["position"],
                "doi": paper["doi"],
                "status": paper["endnote_status"],
                "error": paper["error"],
            }
            for paper in batch["papers"]
            if paper["endnote_status"] != "verified"
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=Path("examples/dtn_acceptance.csv"))
    parser.add_argument("--pdf-dir", type=Path, default=Path("work/dtn-acceptance/pdf"))
    parser.add_argument("--output", type=Path, default=Path("outputs/endnote-live-acceptance.json"))
    args = parser.parse_args()

    library = args.library.resolve()
    if not library.is_file() or not library.with_suffix(".Data").is_dir():
        raise FileNotFoundError(f"Incomplete EndNote library: {library}")
    settings = Settings.load()
    adapter = EndNoteAdapter(settings.endnote_exe, settings.generated_dir, settings.backup_dir)
    if adapter.is_endnote_running():
        raise RuntimeError("EndNote must be closed before the first pass so the backup is consistent")

    fixture_text = args.fixture.resolve().read_text(encoding="utf-8-sig")
    with httpx.Client(base_url=args.base_url, timeout=60.0, follow_redirects=True) as client:
        client.get("/").raise_for_status()
        first_prepared = create_prepared_batch(
            client,
            fixture_text=fixture_text,
            library=library,
            name="DTN & FL EndNote live acceptance pass 1",
            pdf_dir=args.pdf_dir,
        )
        first = commit_and_wait(client, first_prepared["id"])
        first_summary = summarize(first)
        if first_summary["failures"]:
            second = None
        else:
            backup = Path(first["backup_path"])
            if not backup.is_file() or backup.stat().st_size == 0:
                raise RuntimeError(f"EndNote backup verification failed: {backup}")
            second_prepared = create_prepared_batch(
                client,
                fixture_text=fixture_text,
                library=library,
                name="DTN & FL EndNote live acceptance pass 2",
                pdf_dir=args.pdf_dir,
            )
            # The two passes are one acceptance transaction, so the verified
            # pre-first-pass backup is deliberately reused for the idempotency pass.
            Database(settings.database_path).update_batch(second_prepared["id"], backup_path=str(backup))
            second = commit_and_wait(client, second_prepared["id"])

    second_summary = summarize(second) if second else None
    final_xml = settings.generated_dir / "endnote-live-acceptance-final.xml"
    adapter.export_xml(library, final_xml)
    records = parse_endnote_xml(final_xml)
    attachment_checks: dict[str, Any] = {}
    for paper in first["papers"]:
        position = int(paper["position"])
        if position not in PDF_BY_POSITION:
            continue
        matches = match_records(records, metadata_for(paper))
        expected = sha256((args.pdf_dir / PDF_BY_POSITION[position]).resolve())
        matching_paths: list[str] = []
        if len(matches) == 1:
            for attachment in matches[0].attachments:
                resolved = resolve_internal_attachment(library, attachment)
                if resolved and resolved.is_file() and sha256(resolved) == expected:
                    matching_paths.append(str(resolved))
        attachment_checks[str(position)] = {
            "matching_attachment_count": len(matching_paths),
            "paths": matching_paths,
            "passed": len(matching_paths) == 1,
        }

    same_keys = bool(second_summary and first_summary["records"] == second_summary["records"])
    passed = bool(
        not first_summary["failures"]
        and second_summary
        and not second_summary["failures"]
        and same_keys
        and all(check["passed"] for check in attachment_checks.values())
    )
    result = {
        "passed": passed,
        "library": str(library),
        "paper_count": 8,
        "same_record_numbers_on_repeat": same_keys,
        "backup": first_summary["backup_path"],
        "final_xml": str(final_xml),
        "attachment_checks": attachment_checks,
        "first_pass": first_summary,
        "second_pass": second_summary,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
