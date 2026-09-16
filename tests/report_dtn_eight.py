"""Generate the final eight-paper DTN acquisition and Zotero audit report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def enclosure_path(item: dict) -> Path | None:
    href = item.get("links", {}).get("enclosure", {}).get("href", "")
    parsed = urlparse(href)
    if parsed.scheme != "file":
        return None
    value = unquote(parsed.path)
    if len(value) >= 3 and value[0] == "/" and value[2] == ":":
        value = value[1:]
    return Path(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--output", type=Path, default=Path("outputs/dtn-eight-paper-final.json"))
    args = parser.parse_args()

    papers = httpx.Client(timeout=30.0, trust_env=False).get(
        f"{args.base_url}/api/batches/{args.batch_id}"
    ).raise_for_status().json()["papers"]
    results = []
    for paper in papers:
        key = paper.get("record_number")
        children = []
        if key:
            response = httpx.Client(timeout=30.0, trust_env=False).get(
                f"http://127.0.0.1:23119/api/users/0/items/{key}/children",
                headers={"Zotero-API-Version": "3"},
            )
            response.raise_for_status()
            children = response.json()
        pdfs = [item for item in children if item.get("data", item).get("contentType") == "application/pdf"]
        attachments = []
        for item in pdfs:
            data = item.get("data", item)
            path = enclosure_path(item)
            attachments.append(
                {
                    "key": item.get("key") or data.get("key"),
                    "filename": data.get("filename"),
                    "md5": data.get("md5"),
                    "path": str(path) if path else None,
                    "exists": bool(path and path.is_file()),
                    "sha256": sha256(path) if path and path.is_file() else None,
                }
            )
        expected_pdf = paper.get("pdf_status") in {"verified", "accepted"}
        attachment_ok = (
            len(attachments) == 1
            and attachments[0]["exists"]
            and attachments[0]["sha256"] == paper.get("pdf_sha256")
        ) if expected_pdf else len(attachments) == 0
        results.append(
            {
                "position": paper["position"],
                "doi": paper.get("doi"),
                "title": paper.get("title"),
                "status": paper.get("status"),
                "pdf_status": paper.get("pdf_status"),
                "version": paper.get("version"),
                "source_url": paper.get("source_url"),
                "download_path": paper.get("pdf_path"),
                "download_sha256": paper.get("pdf_sha256"),
                "zotero_record_key": key,
                "zotero_pdf_attachment_count": len(attachments),
                "zotero_attachments": attachments,
                "attachment_check_passed": attachment_ok,
                "needs_action": paper.get("needs_action"),
                "error": paper.get("error"),
            }
        )

    obtained = [item for item in results if item["pdf_status"] in {"verified", "accepted"}]
    unresolved = [item for item in results if item["pdf_status"] not in {"verified", "accepted"}]
    report = {
        "batch_id": args.batch_id,
        "paper_count": len(results),
        "pdf_obtained_count": len(obtained),
        "pdf_unresolved_count": len(unresolved),
        "zotero_attachment_checks_passed": all(item["attachment_check_passed"] for item in results),
        "papers": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["zotero_attachment_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
