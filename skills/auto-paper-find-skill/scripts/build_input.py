"""Write paper input and verify it with the target project's real parser."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
from pathlib import Path
import re


FIELDS = ("doi", "title", "year", "author")


def load_parser(project_root: Path):
    source = project_root.resolve() / "paper_endnote" / "inputs.py"
    if not source.is_file():
        raise ValueError(f"Project parser not found: {source}")
    spec = importlib.util.spec_from_file_location("paper_input_contract", source)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load project parser: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_records(records, parser):
    if not isinstance(records, list) or not records:
        raise ValueError("Records must be a nonempty JSON array.")
    cleaned = []
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict) or set(record) - set(FIELDS):
            raise ValueError(f"Record {index}: use only doi, title, year, author.")
        row = {}
        for field in FIELDS:
            value = record.get(field)
            if value is None:
                value = ""
            if field == "year" and isinstance(value, int) and not isinstance(value, bool):
                value = str(value)
            if not isinstance(value, str):
                raise ValueError(f"Record {index}: {field} must be text (year may be an integer).")
            if any(char in value for char in "\r\n\t\ufeff"):
                raise ValueError(f"Record {index}: {field} contains a line break, tab or BOM.")
            row[field] = value.strip() or None
        if row["doi"]:
            # Only remove known wrappers; never silently clip an identifier.
            bare = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", row["doi"], flags=re.I)
            bare = re.sub(r"^doi\s*:\s*", "", bare, flags=re.I).strip().lower()
            normalized = parser.normalize_doi(bare)
            if not normalized or normalized != bare:
                raise ValueError(f"Record {index}: DOI is invalid or changed by the project parser; verify it or use a confirmed title.")
            row["doi"] = normalized
        if not row["doi"] and not row["title"]:
            raise ValueError(f"Record {index}: doi or title is required.")
        if row["year"] is not None:
            if not re.fullmatch(r"(?:18|19|20|21)[0-9]{2}", row["year"]):
                raise ValueError(f"Record {index}: year must be 1800-2199 or empty.")
            row["year"] = int(row["year"])
        cleaned.append(row)
    return cleaned


def render(rows, suffix):
    if suffix == ".csv":
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue(), rows
    if suffix == ".txt":
        lines = [row["doi"] or row["title"] for row in rows]
        expected = [
            {"doi": row["doi"], "title": None if row["doi"] else row["title"], "year": None, "author": None}
            for row in rows
        ]
        return "\n".join(lines) + "\n", expected
    raise ValueError("Output extension must be .csv or .txt.")


def verify(text, expected, parser):
    parsed = parser.parse_input(text)
    actual = [{key: row.get(key) for key in FIELDS} for row in parsed]
    if actual != expected:
        raise ValueError(
            f"Project parser round-trip failed ({len(expected)} expected, {len(actual)} parsed). "
            "Review duplicates, field values and delimiter/header detection; try CSV for TXT inputs."
        )


def build(project_root: Path, records_path: Path, output: Path):
    parser = load_parser(project_root)
    rows = clean_records(json.loads(records_path.read_text(encoding="utf-8-sig")), parser)
    content, expected = render(rows, output.suffix.lower())
    verify(content, expected, parser)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoding = "utf-8-sig" if output.suffix.lower() == ".csv" else "utf-8"
    with output.open("x", encoding=encoding, newline="") as stream:
        stream.write(content)
    verify(output.read_text(encoding="utf-8-sig"), expected, parser)
    return {"output": str(output.resolve()), "records": len(expected), "parser_round_trip": "passed", "bibliographic_verification": "not_performed_by_script"}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--project-root", type=Path, required=True)
    cli.add_argument("--records", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    try:
        result = build(args.project_root, args.records, args.output)
    except (ValueError, OSError, csv.Error) as exc:
        cli.exit(2, f"Input generation failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
