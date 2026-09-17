from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from paper_endnote.db import Database
from paper_endnote.library_files import (
    LibraryFilesError,
    bibliographic_filename,
    copy_pdf_file,
    downloads_library_dir,
    export_batch_pdfs,
    export_endnote_data_pdfs,
    export_zotero_endnote_package,
    export_zotero_pdfs,
    list_batch_pdf_items,
    list_endnote_pdf_items,
    list_zotero_pdf_items,
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

    def test_export_batch_pdfs_only_copies_selected_paper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "db.sqlite3")
            batch_id = db.create_batch(
                name="selective",
                target_library="Selection",
                library_mode="new",
                items=[
                    {"input_text": "First Paper", "title": "First Paper"},
                    {"input_text": "Second Paper", "title": "Second Paper"},
                ],
            )
            papers = db.get_batch(batch_id)["papers"]
            for index, paper in enumerate(papers, start=1):
                source = root / "downloads" / paper["id"] / f"paper-{index}.pdf"
                source.parent.mkdir(parents=True)
                source.write_bytes(MINIMAL_PDF)
                db.update_paper(
                    paper["id"],
                    title=f"Paper {index}",
                    authors_json=[f"Author, Number {index}"],
                    pdf_path=str(source),
                    pdf_status="verified",
                )

            items = list_batch_pdf_items(db, batch_id)
            self.assertEqual([item["id"] for item in items], [paper["id"] for paper in papers])
            self.assertTrue(all(item["available"] for item in items))

            destination = root / "selected-export"
            result = export_batch_pdfs(
                db,
                batch_id,
                destination,
                item_ids={papers[1]["id"]},
            )

            self.assertEqual(result["selected"], 1)
            self.assertEqual(result["copied"], 1)
            self.assertEqual(result["skipped"], 0)
            self.assertEqual(result["files"][0]["paper_id"], papers[1]["id"])
            self.assertEqual(
                [path.name for path in destination.glob("*.pdf")],
                ["Number 2 Author - Paper 2.pdf"],
            )

    def test_export_batch_pdfs_rejects_empty_and_unknown_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "db.sqlite3")
            batch_id = db.create_batch(
                name="selection-errors",
                target_library="Selection",
                library_mode="new",
                items=[{"input_text": "Paper", "title": "Paper"}],
            )
            destination = root / "export"

            with self.assertRaisesRegex(LibraryFilesError, "至少选择"):
                export_batch_pdfs(db, batch_id, destination, item_ids=set())
            with self.assertRaisesRegex(LibraryFilesError, "已变化"):
                export_batch_pdfs(db, batch_id, destination, item_ids={"missing-paper"})

    def test_export_batch_pdfs_rolls_back_files_created_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "db.sqlite3")
            batch_id = db.create_batch(
                name="rollback",
                target_library="Rollback",
                library_mode="new",
                items=[
                    {"input_text": "Paper 1", "title": "Paper 1"},
                    {"input_text": "Paper 2", "title": "Paper 2"},
                ],
            )
            papers = db.get_batch(batch_id)["papers"]
            for index, paper in enumerate(papers, start=1):
                source = root / "downloads" / paper["id"] / f"paper-{index}.pdf"
                source.parent.mkdir(parents=True)
                source.write_bytes(MINIMAL_PDF)
                db.update_paper(
                    paper["id"],
                    title=f"Paper {index}",
                    pdf_path=str(source),
                    pdf_status="verified",
                )

            destination = root / "export"
            destination.mkdir()
            existing = destination / "keep.pdf"
            existing.write_bytes(b"keep")
            calls = 0

            def fail_second_copy(path, destination_dir, metadata=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("copy failed")
                return copy_pdf_file(path, destination_dir, metadata)

            with (
                patch(
                    "paper_endnote.library_files.copy_pdf_file",
                    side_effect=fail_second_copy,
                ),
                self.assertRaisesRegex(OSError, "copy failed"),
            ):
                export_batch_pdfs(db, batch_id, destination)

            self.assertEqual([path.name for path in destination.iterdir()], ["keep.pdf"])
            self.assertEqual(existing.read_bytes(), b"keep")

    def test_copy_pdf_file_removes_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            source.write_bytes(MINIMAL_PDF)
            destination = root / "export"

            def write_then_fail(_source, target):
                Path(target).write_bytes(b"partial")
                raise OSError("disk full")

            with (
                patch(
                    "paper_endnote.library_files.shutil.copy2",
                    side_effect=write_then_fail,
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                copy_pdf_file(source, destination)

            self.assertEqual(list(destination.glob("*.pdf")), [])

    def test_concurrent_copy_reserves_distinct_paths_and_failure_keeps_peer_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            successful_source = root / "successful.pdf"
            failing_source = root / "failing.pdf"
            successful_source.write_bytes(MINIMAL_PDF + b"success")
            failing_source.write_bytes(MINIMAL_PDF + b"failure")
            destination = root / "export"
            barrier = threading.Barrier(2)
            success_written = threading.Event()

            def coordinated_copy(source, target):
                barrier.wait(timeout=5)
                if Path(source) == failing_source:
                    if not success_written.wait(timeout=5):
                        raise AssertionError("successful peer copy did not finish")
                    Path(target).write_bytes(b"partial")
                    raise OSError("simulated concurrent failure")
                Path(target).write_bytes(Path(source).read_bytes())
                success_written.set()
                return str(target)

            metadata = {"title": "Same Export Name"}
            with patch("paper_endnote.library_files.shutil.copy2", side_effect=coordinated_copy):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    successful = executor.submit(
                        copy_pdf_file, successful_source, destination, metadata
                    )
                    failing = executor.submit(
                        copy_pdf_file, failing_source, destination, metadata
                    )
                    successful_result = successful.result(timeout=10)
                    with self.assertRaisesRegex(OSError, "simulated concurrent failure"):
                        failing.result(timeout=10)

            remaining = list(destination.glob("*.pdf"))
            self.assertEqual(remaining, [Path(successful_result["path"])])
            self.assertEqual(remaining[0].read_bytes(), MINIMAL_PDF + b"success")

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

    def test_export_endnote_data_pdfs_only_copies_selected_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "DTN.enl"
            library.write_bytes(b"")
            pdf_root = library.with_suffix(".Data") / "PDF"
            first = pdf_root / "REF1" / "first.pdf"
            second = pdf_root / "REF2" / "second.pdf"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(MINIMAL_PDF)
            second.write_bytes(MINIMAL_PDF)

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
            connection.executemany(
                "INSERT INTO refs VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "Alpha, Alice", "2023", "First EndNote Paper", "Journal A", "10.1000/first", ""),
                    (2, "Beta, Bob", "2024", "Second EndNote Paper", "Journal B", "10.1000/second", ""),
                ],
            )
            connection.executemany(
                "INSERT INTO file_res VALUES (?, ?, 'pdf', 0)",
                [
                    (1, "internal-pdf://REF1/first.pdf"),
                    (2, "internal-pdf://REF2/second.pdf"),
                ],
            )
            connection.commit()
            connection.close()

            items = list_endnote_pdf_items(library)
            self.assertEqual([item["id"] for item in items], ["ref:1", "ref:2"])
            self.assertTrue(all(item["available"] for item in items))

            destination = root / "selected-endnote"
            result = export_endnote_data_pdfs(
                library,
                destination,
                item_ids={"ref:2"},
            )

            self.assertEqual(result["selected"], 1)
            self.assertEqual(result["copied"], 1)
            self.assertEqual(result["skipped"], 0)
            self.assertEqual(result["files"][0]["item_id"], "ref:2")
            self.assertEqual(
                [path.name for path in destination.glob("*.pdf")],
                ["Bob Beta - Second EndNote Paper.pdf"],
            )

    def test_export_endnote_orphan_selection_without_sdb(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "Legacy.enl"
            library.write_bytes(b"")
            source = library.with_suffix(".Data") / "PDF" / "ITEM1" / "orphan.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(MINIMAL_PDF)

            item = list_endnote_pdf_items(library)[0]
            destination = root / "selected"
            result = export_endnote_data_pdfs(
                library,
                destination,
                item_ids={item["id"]},
            )

            self.assertEqual(item["id"], "file:ITEM1/orphan.pdf")
            self.assertEqual(result["copied"], 1)
            self.assertEqual(result["files"][0]["item_id"], item["id"])
            self.assertTrue((destination / "orphan.pdf").is_file())

    def test_endnote_scan_rejects_pdf_symlink_resolving_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "Boundary.enl"
            library.write_bytes(b"")
            pdf_root = library.with_suffix(".Data") / "PDF"
            legitimate = pdf_root / "ITEM1" / "inside.pdf"
            legitimate.parent.mkdir(parents=True)
            legitimate.write_bytes(MINIMAL_PDF + b"inside")
            outside = root / "outside.pdf"
            outside.write_bytes(MINIMAL_PDF + b"outside")
            link = pdf_root / "outside-link.pdf"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")

            items = list_endnote_pdf_items(library)
            self.assertEqual([item["id"] for item in items], ["file:ITEM1/inside.pdf"])

            destination = root / "export"
            result = export_endnote_data_pdfs(library, destination)
            self.assertEqual(result["copied"], 1)
            self.assertEqual([path.name for path in destination.glob("*.pdf")], ["ITEM1_inside.pdf"])
            self.assertNotEqual(next(destination.glob("*.pdf")).read_bytes(), outside.read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_endnote_scan_rejects_junction_resolving_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "Boundary.enl"
            library.write_bytes(b"")
            pdf_root = library.with_suffix(".Data") / "PDF"
            pdf_root.mkdir(parents=True)
            outside_dir = root / "outside"
            outside_dir.mkdir()
            outside = outside_dir / "outside.pdf"
            outside.write_bytes(MINIMAL_PDF + b"outside")
            junction = pdf_root / "outside-junction"
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(f"junction creation is unavailable: {created.stderr}")
            try:
                self.assertEqual(list_endnote_pdf_items(library), [])
                destination = root / "export"
                result = export_endnote_data_pdfs(library, destination)
                self.assertEqual(result["copied"], 0)
                self.assertFalse(destination.exists())
            finally:
                if junction.exists():
                    os.rmdir(junction)

    def test_export_endnote_selection_rolls_back_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "Legacy.enl"
            library.write_bytes(b"")
            pdf_root = library.with_suffix(".Data") / "PDF"
            for folder in ("ITEM1", "ITEM2"):
                source = pdf_root / folder / f"{folder.casefold()}.pdf"
                source.parent.mkdir(parents=True)
                source.write_bytes(MINIMAL_PDF)
            item_ids = {item["id"] for item in list_endnote_pdf_items(library)}
            destination = root / "selected"
            calls = 0

            def fail_second_copy(path, destination_dir, metadata=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("copy failed")
                return copy_pdf_file(path, destination_dir, metadata)

            with (
                patch(
                    "paper_endnote.library_files.copy_pdf_file",
                    side_effect=fail_second_copy,
                ),
                self.assertRaisesRegex(OSError, "copy failed"),
            ):
                export_endnote_data_pdfs(
                    library,
                    destination,
                    item_ids=item_ids,
                )

            self.assertEqual(list(destination.glob("*.pdf")), [])

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


class _FakeSelectiveZoteroExport:
    def __init__(self, pdfs: dict[str, Path], *, fail_key: str | None = None) -> None:
        self.pdfs = pdfs
        self.fail_key = fail_key
        self.export_calls: list[str] = []

    async def iter_collection_items(self, name: str):
        for key in self.pdfs:
            yield {"data": {"key": key, "itemType": "journalArticle"}}
        yield {"data": {"key": "NOTE", "itemType": "note"}}

    async def export_row(self, key: str, fallback_metadata=None):
        self.export_calls.append(key)
        if key == self.fail_key:
            raise RuntimeError("Zotero row failed")
        number = 1 if key == "ITEM1" else 2
        return {
            "zotero_key": key,
            "attachment_key": f"ATT{number}",
            "metadata": {
                "title": f"Zotero Paper {number}",
                "doi": f"10.1000/zotero-{number}",
                "year": 2020 + number,
                "authors": [f"Author, Zotero {number}"],
                "journal": "A Journal",
            },
            "pdf_path": self.pdfs[key],
        }


class ZoteroEndNoteExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_zotero_pdfs_does_not_resolve_unselected_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdfs = {
                "ITEM1": root / "item-1.pdf",
                "ITEM2": root / "item-2.pdf",
            }
            for path in pdfs.values():
                path.write_bytes(MINIMAL_PDF)
            zotero = _FakeSelectiveZoteroExport(pdfs)

            items = await list_zotero_pdf_items(zotero, "Selection")
            self.assertEqual([item["id"] for item in items], ["ITEM1", "ITEM2"])
            self.assertTrue(all(item["available"] for item in items))

            zotero.export_calls.clear()
            destination = root / "selected-zotero"
            result = await export_zotero_pdfs(
                zotero,
                "Selection",
                destination,
                item_ids={"ITEM2"},
            )

            self.assertEqual(zotero.export_calls, ["ITEM2"])
            self.assertEqual(result["selected"], 1)
            self.assertEqual(result["copied"], 1)
            self.assertEqual(result["skipped"], 0)
            self.assertEqual(result["files"][0]["zotero_key"], "ITEM2")
            self.assertEqual(
                [path.name for path in destination.glob("*.pdf")],
                ["Zotero 2 Author - Zotero Paper 2.pdf"],
            )

    async def test_export_zotero_pdfs_rolls_back_before_row_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdfs = {
                "ITEM1": root / "item-1.pdf",
                "ITEM2": root / "item-2.pdf",
            }
            for path in pdfs.values():
                path.write_bytes(MINIMAL_PDF)
            zotero = _FakeSelectiveZoteroExport(pdfs, fail_key="ITEM2")
            destination = root / "selected-zotero"

            with self.assertRaisesRegex(RuntimeError, "Zotero row failed"):
                await export_zotero_pdfs(
                    zotero,
                    "Selection",
                    destination,
                    item_ids={"ITEM1", "ITEM2"},
                )

            self.assertEqual(zotero.export_calls, ["ITEM1", "ITEM2"])
            self.assertEqual(list(destination.glob("*.pdf")), [])

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
