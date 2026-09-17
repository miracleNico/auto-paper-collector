from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_endnote.library_files import (
    read_endnote_library_records,
    sync_endnote_to_zotero,
)


MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _build_endnote_library(
    root: Path, *, include_unsupported: bool = False
) -> tuple[Path, Path, Path]:
    library = root / "Source Library.enl"
    library.write_bytes(b"")

    pdf_dir = library.with_suffix(".Data") / "PDF" / "REF1"
    pdf_dir.mkdir(parents=True)
    primary_pdf = pdf_dir / "primary.pdf"
    supplemental_pdf = pdf_dir / "supplement.pdf"
    primary_pdf.write_bytes(MINIMAL_PDF)
    supplemental_pdf.write_bytes(MINIMAL_PDF + b"supplement")
    (root / "outside.pdf").write_bytes(MINIMAL_PDF + b"outside")

    sdb_dir = library.with_suffix(".Data") / "sdb"
    sdb_dir.mkdir()
    connection = sqlite3.connect(sdb_dir / "sdb.eni")
    connection.executescript(
        """
        CREATE TABLE refs (
            id INTEGER PRIMARY KEY,
            trash_state INTEGER,
            reference_type INTEGER,
            author TEXT,
            year TEXT,
            title TEXT,
            pages TEXT,
            secondary_title TEXT,
            volume TEXT,
            number TEXT,
            url TEXT,
            abstract TEXT,
            keywords TEXT,
            short_title TEXT,
            language TEXT,
            access_date TEXT,
            publisher TEXT,
            isbn TEXT,
            electronic_resource_number TEXT
        );
        CREATE TABLE file_res (
            refs_id INTEGER,
            file_path TEXT,
            file_type INTEGER,
            file_pos INTEGER
        );
        """
    )
    rows = [
        (
            1,
            0,
            0,
            "Doe, Jane//Smith, John",
            "Published 2024",
            "Created Paper",
            "10-20",
            "Complete Journal",
            "12",
            "3",
            "https://example.test/created",
            "A complete abstract.",
            "alpha; beta//gamma\ndelta",
            "Created",
            "en",
            "2026-09-17",
            "Complete Publisher",
            "978-1-23456-789-0",
            "https://doi.org/10.1000/CREATE",
        ),
        (
            2,
            0,
            0,
            "Failure, Fiona",
            "2023",
            "Broken Paper",
            "",
            "Failure Journal",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "10.1000/broken",
        ),
        (
            3,
            0,
            0,
            "Existing, Erin",
            "2022",
            "Existing Paper",
            "",
            "Existing Journal",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "10.1000/existing",
        ),
        (
            4,
            0,
            0,
            "Unknown, Uma",
            "2021",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ),
        (
            5,
            1,
            0,
            "Trash, Terry",
            "2020",
            "Trashed Paper",
            "",
            "Trash Journal",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "10.1000/trashed",
        ),
    ]
    if include_unsupported:
        rows.append(
            (
                6,
                0,
                6,
                "Unsupported, Una",
                "2019",
                "Unsupported Reference",
                "",
                "",
                "",
                "",
                "https://example.test/unsupported",
                "",
                "",
                "",
                "en",
                "",
                "Unsupported Publisher",
                "978-0-00000-000-6",
                "10.1000/unsupported",
            )
        )
    connection.executemany(
        """
        INSERT INTO refs (
            id, trash_state, reference_type, author, year, title, pages, secondary_title,
            volume, number, url, abstract, keywords, short_title, language,
            access_date, publisher, isbn, electronic_resource_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.executemany(
        "INSERT INTO file_res (refs_id, file_path, file_type, file_pos) VALUES (?, ?, ?, ?)",
        [
            (1, "REF1/primary.pdf", 1, 0),
            (1, "internal-pdf://REF1/supplement.pdf", 1, 1),
            (3, "../../outside.pdf", 1, 0),
            (5, "internal-pdf://REF1/primary.pdf", 1, 0),
            *(
                [(6, "internal-pdf://REF1/primary.pdf", 1, 0)]
                if include_unsupported
                else []
            ),
        ],
    )
    connection.commit()
    connection.close()
    return library, primary_pdf.resolve(), supplemental_pdf.resolve()


class _FakeZotero:
    def __init__(self) -> None:
        self.ensure_calls: list[tuple[str, bool]] = []
        self.commit_calls: list[tuple[str, dict, Path | None]] = []
        self.attach_calls: list[tuple[str, Path]] = []

    async def ensure_collection(self, name: str, *, create: bool) -> str:
        self.ensure_calls.append((name, create))
        return "COLL1234"

    async def commit_paper(
        self, collection_key: str, metadata: dict, pdf_path: Path | None
    ) -> dict:
        if pdf_path is not None:
            raise AssertionError("EndNote sync must commit metadata before attaching PDFs")
        self.commit_calls.append((collection_key, metadata, pdf_path))
        title = metadata.get("title")
        if title == "Broken Paper":
            raise RuntimeError("simulated item failure")
        if title == "Created Paper":
            return {
                "created": True,
                "record_number": "NEWITEM1",
            }
        if title == "Existing Paper":
            return {
                "created": False,
                "attached": False,
                "record_number": "EXISTING1",
            }
        raise AssertionError(f"unexpected record: {title!r}")

    async def attach_pdf(self, item_key: str, pdf_path: Path) -> dict:
        self.attach_calls.append((item_key, Path(pdf_path)))
        return {"attached": True, "existing": False, "attachment_key": "ATTACH2"}


class EndNoteZoteroSyncTests(unittest.IsolatedAsyncioTestCase):
    def test_read_endnote_library_records_reads_active_complete_record_and_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library, primary_pdf, supplemental_pdf = _build_endnote_library(Path(directory))

            records = read_endnote_library_records(library)

        self.assertEqual([record["id"] for record in records], ["ref:1", "ref:2", "ref:3", "ref:4"])
        complete = records[0]
        self.assertEqual(complete["ref_id"], "1")
        self.assertEqual(complete["pdf_paths"], [primary_pdf, supplemental_pdf])
        self.assertEqual(records[2]["pdf_paths"], [])
        self.assertEqual(
            complete["metadata"],
            {
                "authors": ["Doe, Jane", "Smith, John"],
                "title": "Created Paper",
                "year": 2024,
                "doi": "10.1000/create",
                "journal": "Complete Journal",
                "volume": "12",
                "issue": "3",
                "pages": "10-20",
                "url": "https://example.test/created",
                "abstract": "A complete abstract.",
                "short_title": "Created",
                "language": "en",
                "access_date": "2026-09-17",
                "tags": ["alpha", "beta", "gamma", "delta"],
                "publisher": "Complete Publisher",
                "isbn": "978-1-23456-789-0",
                "endnote_reference_type": 0,
                "item_type": "journalArticle",
                "library_catalog": "EndNote",
            },
        )
        self.assertNotIn("ref:5", {record["id"] for record in records})

    async def test_sync_counts_create_match_failure_skip_and_routes_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library, primary_pdf, supplemental_pdf = _build_endnote_library(Path(directory))
            zotero = _FakeZotero()

            with patch(
                "paper_endnote.library_files.endnote_desktop_running",
                return_value=False,
            ):
                result = await sync_endnote_to_zotero(zotero, library, "Synced Papers")

        self.assertEqual(zotero.ensure_calls, [("Synced Papers", True)])
        self.assertEqual(
            [metadata["title"] for _, metadata, _ in zotero.commit_calls],
            ["Created Paper", "Broken Paper", "Existing Paper"],
        )
        self.assertEqual(zotero.commit_calls[0][0], "COLL1234")
        self.assertTrue(all(pdf_path is None for _, _, pdf_path in zotero.commit_calls))
        self.assertEqual(
            zotero.attach_calls,
            [("NEWITEM1", primary_pdf), ("NEWITEM1", supplemental_pdf)],
        )

        self.assertEqual(result["record_count"], 4)
        self.assertEqual(result["synced"], 2)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["pdf_attached"], 2)
        self.assertEqual(result["pdf_existing"], 0)
        self.assertEqual(result["errors"], [
            {
                "id": "ref:2",
                "title": "Broken Paper",
                "error": "simulated item failure",
            }
        ])
        self.assertEqual(
            [(row["id"], row["status"]) for row in result["records"]],
            [
                ("ref:1", "synced"),
                ("ref:2", "failed"),
                ("ref:3", "synced"),
                ("ref:4", "skipped"),
            ],
        )

    async def test_sync_explicitly_skips_unsupported_reference_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            library, _, _ = _build_endnote_library(
                Path(directory), include_unsupported=True
            )
            zotero = _FakeZotero()

            with patch(
                "paper_endnote.library_files.endnote_desktop_running",
                return_value=False,
            ):
                result = await sync_endnote_to_zotero(zotero, library, "Synced Papers")

        unsupported = next(row for row in result["records"] if row["id"] == "ref:6")
        self.assertEqual(unsupported["status"], "skipped")
        self.assertEqual(unsupported["title"], "Unsupported Reference")
        self.assertEqual(unsupported["error"], "暂不支持的 EndNote 题录类型：6")
        self.assertNotIn(
            "Unsupported Reference",
            [metadata["title"] for _, metadata, _ in zotero.commit_calls],
        )
        self.assertEqual(result["record_count"], 5)
        self.assertEqual(result["skipped"], 2)


if __name__ == "__main__":
    unittest.main()
