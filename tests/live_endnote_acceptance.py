"""Live check that a Zotero-committed batch can export an EndNote import package.

This does not drive the EndNote desktop UI. It calls the local web service, which
reads PDFs from Zotero storage and writes XML/PDF/RIS into generated/.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from paper_endnote.endnote import parse_endnote_xml, resolve_export_attachment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--output", type=Path, default=Path("outputs/endnote-export-acceptance.json"))
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=120.0, follow_redirects=True) as client:
        client.get("/").raise_for_status()
        export = client.post(f"/api/batches/{args.batch_id}/export-endnote")
        export.raise_for_status()
        manifest = export.json()
        batch = client.get(f"/api/batches/{args.batch_id}").json()

    export_dir = Path(batch["endnote_export_path"])
    records = parse_endnote_xml(export_dir / "records.xml")
    pdfs = [
        str(resolve_export_attachment(export_dir, attachment))
        for record in records
        for attachment in record.attachments
        if resolve_export_attachment(export_dir, attachment)
    ]
    result = {
        "passed": bool(records) and Path(manifest["zip"]).is_file(),
        "batch_id": args.batch_id,
        "record_count": len(records),
        "resolved_pdfs": pdfs,
        "manifest": manifest,
        "export_dir": str(export_dir),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
