from __future__ import annotations

import csv
import io
from typing import Any


REPORT_FIELDS = [
    "position", "input_text", "doi", "title", "year", "journal", "status",
    "metadata_status", "pdf_status", "endnote_status", "version", "source_url",
    "pdf_path", "record_number", "needs_action", "error",
]


def batch_csv(batch: dict[str, Any]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=REPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for paper in batch.get("papers", []):
        writer.writerow({key: paper.get(key, "") for key in REPORT_FIELDS})
    return "\ufeff" + output.getvalue()
