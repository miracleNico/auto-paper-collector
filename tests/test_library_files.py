from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from paper_endnote.db import Database
from paper_endnote.library_files import (
    LibraryFilesError,
    bibliographic_filename,
    downloads_library_dir,
    export_batch_pdfs,
    export_endnote_data_pdfs,
    export_zotero_endnote_package,
    rename_batch_pdfs,
    rename_endnote_pdfs,
    rename_pdf_file,
    resolve_export_dir,
)

MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class LibraryFilesTests(unittest.TestCase):
    def test_bibliographic_filename_uses_author_dash_title(self) -> None:
        name = bibliographic_filename(
            {"authors": ["Cao, Yue"], "year": 2013, "title": "Routing in DTN", "doi": "10.1109/surv.2012.042512.00053"}
        )
        self.assertEqual(name, "Yue Cao - Routing in DTN.pdf")
        self.assertEqual(
            bibliographic_filename({"authors": ["Anna Zhivtsova"], "title": "A Survey of Delay-Oriented Policies"}),
            "Anna Zhivtsova - A Survey of Delay-Oriented Policies.pdf",
        )

    def test_rename_and_export_batch_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "db.sqlite3")
            batch_id = db.create_batch(
                name="t",
                target_library="DTN",
                library_mode="new",
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a", "title": "Example Paper"}],
            )
            paper_id = db.get_batch(batch_id)["papers"][0]["id"]
            source = root / "downloads" / paper_id / "institution-main.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(MINIMAL_PDF)
            db.update_paper(
                paper_id,
                doi="10.1000/a",
                title="Example Paper",
                year=2024,
                authors_json=["Doe, Jane"],
                pdf_path=str(source),
                pdf_status="verified",
            )
            renamed = rename_batch_pdfs(db, batch_id)
            self.assertEqual(renamed["renamed"], 1)
            paper = db.get_paper(paper_id)
            path = Path(paper["pdf_path"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.name, "Jane Doe - Example Paper.pdf")
            self.assertFalse(source.exists())

            destination = root / "export"
            exported = export_batch_pdfs(db, batch_id, destination)
            self.assertEqual(exported["copied"], 1)
            self.assertTrue((destination / "Jane Doe - Example Paper.pdf").is_file())
            self.assertTrue(path.is_file())

    def test_rename_same_name_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Jane Doe - Example.pdf"
            path.write_bytes(MINIMAL_PDF)
            result = rename_pdf_file(path, {"authors": ["Doe, Jane"], "year": 2024, "title": "Example"})
            self.assertFalse(result["renamed"])
            self.assertTrue(path.is_file())

    def test_downloads_library_dir_uses_lib_name(self) -> None:
        path = downloads_library_dir("DTN")
        self.assertEqual(path.name, "DTN")
        self.assertEqual(path.parent.name, "Downloads")
        self.assertTrue(path.is_dir())
        with self.assertRaises(LibraryFilesError):
            resolve_export_dir("relative/out")

    def test_export_endnote_data_pdfs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "DTN.enl"
            library.write_bytes(b"")
            stored = library.with_suffix(".Data") / "PDF" / "ITEM1"
            stored.mkdir(parents=True)
            (stored / "old.pdf").write_bytes(MINIMAL_PDF)
            destination = root / "out"
            result = export_endnote_data_pdfs(library, destination)
            self.assertEqual(result["copied"], 1)
            self.assertTrue((destination / "ITEM1_old.pdf").is_file())

    def test_rename_endnote_pdfs_updates_sdb_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "DTN.enl"
            library.write_bytes(b"")
            pdf_dir = library.with_suffix(".Data") / "PDF" / "12345"
            pdf_dir.mkdir(parents=True)
            source = pdf_dir / "old.pdf"
            source.write_bytes(MINIMAL_PDF)
            sdb_dir = library.with_suffix(".Data") / "sdb"
            sdb_dir.mkdir()
            connection = sqlite3.connect(sdb_dir / "sdb.eni")
            connection.execute(
                "CREATE TABLE refs (id INTEGER PRIMARY KEY, author TEXT, year TEXT, title TEXT, "
                "secondary_title TEXT, electronic_resource_number TEXT, url TEXT)"
            )
            connection.execute(
                "CREATE TABLE file_res (refs_id INTEGER, file_path TEXT, file_type TEXT, file_pos INTEGER)"
            )
            connection.execute(
                "INSERT INTO refs VALUES (1, 'Doe, Jane', '2024', 'Example Paper', 'A Journal', "
                "'10.1000/example', 'internal-pdf://12345/old.pdf')"
            )
            connection.execute(
                "INSERT INTO file_res VALUES (1, 'internal-pdf://12345/old.pdf', 'pdf', 0)"
            )
            connection.commit()
            connection.close()

            result = rename_endnote_pdfs(library, require_closed=False)
            self.assertEqual(result["renamed"], 1)
            renamed = pdf_dir / "Jane Doe - Example Paper.pdf"
            self.assertTrue(renamed.is_file())
            self.assertFalse(source.exists())
            stored_db = sqlite3.connect(sdb_dir / "sdb.eni")
            try:
                stored = stored_db.execute(
                    "SELECT file_path, url FROM file_res JOIN refs ON refs.id = file_res.refs_id"
                ).fetchone()
            finally:
                stored_db.close()
            self.assertEqual(stored[0], "internal-pdf://12345/Jane Doe - Example Paper.pdf")
            self.assertEqual(stored[1], "internal-pdf://12345/Jane Doe - Example Paper.pdf")


class _FakeZoteroExport:
    def __init__(self, pdf: Path) -> None:
        self.pdf = pdf

    async def iter_collection_items(self, name: str):
        yield {"data": {"key": "ITEMTOP", "itemType": "journalArticle"}}
        yield {"data": {"key": "SKIPME", "itemType": "note"}}

    async def export_row(self, key: str, fallback_metadata=None):
        return {
            "zotero_key": key,
            "attachment_key": "ATTKEY",
            "metadata": {
                "title": "Example Paper",
                "doi": "10.1000/example",
                "year": 2024,
                "authors": ["Doe, Jane"],
                "journal": "A Journal",
            },
            "pdf_path": self.pdf,
        }


class ZoteroEndNoteExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_zotero_endnote_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "stored.pdf"
            pdf.write_bytes(MINIMAL_PDF)
            destination = root / "Downloads" / "DTN"
            extra = destination / "keep-me.pdf"
            extra.parent.mkdir(parents=True)
            extra.write_bytes(MINIMAL_PDF)
            result = await export_zotero_endnote_package(_FakeZoteroExport(pdf), "DTN", destination)
            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["pdf_count"], 1)
            self.assertTrue((destination / "records.xml").is_file())
            self.assertTrue(extra.is_file())
            self.assertEqual(result["destination"], str(destination))


if __name__ == "__main__":
    unittest.main()
