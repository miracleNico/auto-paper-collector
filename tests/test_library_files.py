from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
    rename_zotero_pdfs,
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

    def test_rename_endnote_pdfs_skips_attachment_path_outside_pdf_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "Boundary.enl"
            library.write_bytes(b"")
            pdf_root = library.with_suffix(".Data") / "PDF"
            pdf_root.mkdir(parents=True)
            outside = root / "outside.pdf"
            outside.write_bytes(MINIMAL_PDF)
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
            escaped = "internal-pdf://../../outside.pdf"
            connection.execute(
                "INSERT INTO refs VALUES (1, 'Doe, Jane', '2024', 'Outside Paper', 'A Journal', "
                "'10.1000/outside', ?)",
                (escaped,),
            )
            connection.execute(
                "INSERT INTO file_res VALUES (1, ?, 'pdf', 0)",
                (escaped,),
            )
            connection.commit()
            connection.close()

            result = rename_endnote_pdfs(library, require_closed=False)

            self.assertEqual(result["renamed"], 0)
            self.assertEqual(result["skipped"], 1)
            self.assertTrue(outside.is_file())
            self.assertEqual(outside.read_bytes(), MINIMAL_PDF)
            stored_db = sqlite3.connect(sdb_dir / "sdb.eni")
            try:
                stored = stored_db.execute("SELECT file_path FROM file_res").fetchone()[0]
            finally:
                stored_db.close()
            self.assertEqual(stored, escaped)

    def test_rename_endnote_pdfs_skips_non_pdf_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "Mixed.enl"
            library.write_bytes(b"")
            attachment_dir = library.with_suffix(".Data") / "PDF" / "ITEM1"
            attachment_dir.mkdir(parents=True)
            document = attachment_dir / "notes.docx"
            document.write_bytes(b"not a PDF")
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
                "INSERT INTO refs VALUES (1, 'Doe, Jane', '2024', 'Notes', '', '', '')"
            )
            connection.execute(
                "INSERT INTO file_res VALUES (1, 'internal-pdf://ITEM1/notes.docx', 'docx', 0)"
            )
            connection.commit()
            connection.close()

            result = rename_endnote_pdfs(library, require_closed=False)

            self.assertEqual(result["renamed"], 0)
            self.assertEqual(result["skipped"], 1)
            self.assertTrue(document.is_file())
            self.assertFalse((attachment_dir / "Jane Doe - Notes.pdf").exists())

    def test_rename_endnote_pdfs_rolls_back_prior_files_on_later_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "Rollback.enl"
            library.write_bytes(b"")
            pdf_root = library.with_suffix(".Data") / "PDF"
            originals: list[Path] = []
            for index in (1, 2):
                folder = pdf_root / f"ITEM{index}"
                folder.mkdir(parents=True)
                source = folder / f"old-{index}.pdf"
                source.write_bytes(MINIMAL_PDF)
                originals.append(source)
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
            for index in (1, 2):
                stored = f"internal-pdf://ITEM{index}/old-{index}.pdf"
                connection.execute(
                    "INSERT INTO refs VALUES (?, 'Doe, Jane', '2024', ?, '', '', ?)",
                    (index, f"Paper {index}", stored),
                )
                connection.execute(
                    "INSERT INTO file_res VALUES (?, ?, 'pdf', 0)",
                    (index, stored),
                )
            connection.commit()
            connection.close()
            calls = 0

            def fail_second_rename(path, metadata):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second rename failed")
                return rename_pdf_file(path, metadata)

            with (
                patch(
                    "paper_endnote.library_files.rename_pdf_file",
                    side_effect=fail_second_rename,
                ),
                self.assertRaisesRegex(OSError, "second rename failed"),
            ):
                rename_endnote_pdfs(library, require_closed=False)

            self.assertTrue(all(path.is_file() for path in originals))
            self.assertFalse((pdf_root / "ITEM1" / "Jane Doe - Paper 1.pdf").exists())
            stored_db = sqlite3.connect(sdb_dir / "sdb.eni")
            try:
                stored_paths = [
                    row[0]
                    for row in stored_db.execute(
                        "SELECT file_path FROM file_res ORDER BY rowid"
                    ).fetchall()
                ]
            finally:
                stored_db.close()
            self.assertEqual(
                stored_paths,
                [
                    "internal-pdf://ITEM1/old-1.pdf",
                    "internal-pdf://ITEM2/old-2.pdf",
                ],
            )

    def test_rename_endnote_pdfs_updates_all_rows_sharing_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "Shared.enl"
            library.write_bytes(b"")
            pdf_dir = library.with_suffix(".Data") / "PDF" / "SHARED"
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
            stored = "internal-pdf://SHARED/old.pdf"
            for ref_id in (1, 2):
                connection.execute(
                    "INSERT INTO refs VALUES (?, 'Doe, Jane', '2024', 'Shared Paper', '', '', ?)",
                    (ref_id, stored),
                )
                connection.execute(
                    "INSERT INTO file_res VALUES (?, ?, 'pdf', 0)",
                    (ref_id, stored),
                )
            connection.commit()
            connection.close()

            result = rename_endnote_pdfs(library, require_closed=False)

            renamed = pdf_dir / "Jane Doe - Shared Paper.pdf"
            self.assertEqual(result["renamed"], 1)
            self.assertEqual(result["skipped"], 0)
            self.assertEqual(result["files"][0]["refs_ids"], [1, 2])
            self.assertTrue(renamed.is_file())
            self.assertFalse(source.exists())
            stored_db = sqlite3.connect(sdb_dir / "sdb.eni")
            try:
                rows = stored_db.execute(
                    "SELECT file_path, url FROM file_res "
                    "JOIN refs ON refs.id = file_res.refs_id ORDER BY refs.id"
                ).fetchall()
            finally:
                stored_db.close()
            self.assertEqual(
                rows,
                [
                    (
                        "internal-pdf://SHARED/Jane Doe - Shared Paper.pdf",
                        "internal-pdf://SHARED/Jane Doe - Shared Paper.pdf",
                    ),
                    (
                        "internal-pdf://SHARED/Jane Doe - Shared Paper.pdf",
                        "internal-pdf://SHARED/Jane Doe - Shared Paper.pdf",
                    ),
                ],
            )

    def test_rename_endnote_pdfs_keeps_all_url_links_for_multiple_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "Multiple.enl"
            library.write_bytes(b"")
            pdf_root = library.with_suffix(".Data") / "PDF"
            for folder, filename in (("FIRST", "first.pdf"), ("SECOND", "second.pdf")):
                attachment = pdf_root / folder / filename
                attachment.parent.mkdir(parents=True)
                attachment.write_bytes(MINIMAL_PDF)
            sdb_dir = library.with_suffix(".Data") / "sdb"
            sdb_dir.mkdir()
            connection = sqlite3.connect(sdb_dir / "sdb.eni")
            connection.execute(
                "CREATE TABLE refs (id INTEGER PRIMARY KEY, author TEXT, year TEXT, title TEXT, "
                "secondary_title TEXT, electronic_resource_number TEXT, url TEXT)"
            )
            # Deliberately omit legacy optional file_res columns such as
            # file_type to cover older/variant EndNote databases.
            connection.execute(
                "CREATE TABLE file_res (refs_id INTEGER, file_path TEXT)"
            )
            first = "internal-pdf://FIRST/first.pdf"
            second = "internal-pdf://SECOND/second.pdf"
            connection.execute(
                "INSERT INTO refs VALUES (1, 'Doe, Jane', '2024', 'Multiple Files', '', '', ?)",
                (f"{first};{second}",),
            )
            connection.execute("INSERT INTO file_res VALUES (1, ?)", (first,))
            connection.execute("INSERT INTO file_res VALUES (1, ?)", (second,))
            connection.commit()
            connection.close()

            result = rename_endnote_pdfs(library, require_closed=False)

            expected_name = "Jane Doe - Multiple Files.pdf"
            self.assertEqual(result["renamed"], 2)
            self.assertTrue((pdf_root / "FIRST" / expected_name).is_file())
            self.assertTrue((pdf_root / "SECOND" / expected_name).is_file())
            stored_db = sqlite3.connect(sdb_dir / "sdb.eni")
            try:
                stored_paths = [
                    row[0]
                    for row in stored_db.execute(
                        "SELECT file_path FROM file_res ORDER BY rowid"
                    ).fetchall()
                ]
                url = stored_db.execute("SELECT url FROM refs WHERE id = 1").fetchone()[0]
            finally:
                stored_db.close()
            self.assertEqual(
                stored_paths,
                [
                    f"internal-pdf://FIRST/{expected_name}",
                    f"internal-pdf://SECOND/{expected_name}",
                ],
            )
            self.assertEqual(
                url,
                f"internal-pdf://FIRST/{expected_name};"
                f"internal-pdf://SECOND/{expected_name}",
            )


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


class _FakeScopedZoteroExport:
    def __init__(self, pdf: Path) -> None:
        self.pdf = pdf
        self.iter_calls: list[tuple[str, str]] = []
        self.export_calls: list[tuple[str, str]] = []
        self.rename_calls: list[tuple[str, str, int | None, str]] = []

    async def iter_library_items(self, *, library_id: str, collection_key: str):
        self.iter_calls.append((library_id, collection_key))
        yield {"data": {"key": "GROUPITEM", "itemType": "journalArticle"}}
        yield {"data": {"key": "GROUPNOTE", "itemType": "note"}}

    async def iter_collection_items(self, name: str):
        raise AssertionError(f"legacy collection lookup used unexpectedly: {name}")

    async def export_row(self, key: str, fallback_metadata=None, *, library_id: str = "user:0"):
        self.export_calls.append((key, library_id))
        return {
            "zotero_key": key,
            "attachment_key": "GROUPATT",
            "attachment_version": 7,
            "metadata": {
                "title": "Scoped Paper",
                "doi": "10.1000/scoped",
                "year": 2025,
                "authors": ["Doe, Jane"],
                "journal": "A Journal",
            },
            "pdf_path": self.pdf,
        }

    async def rename_attachment_filename(
        self,
        attachment_key: str,
        filename: str,
        version: int | None,
        *,
        library_id: str = "user:0",
    ) -> None:
        self.rename_calls.append((attachment_key, filename, version, library_id))

    async def attachment_filename(
        self, attachment_key: str, *, library_id: str = "user:0"
    ) -> str:
        return self.pdf.name


class ZoteroEndNoteExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_zotero_whole_library_scope_must_be_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "paper.pdf"
            pdf.write_bytes(MINIMAL_PDF)
            zotero = _FakeScopedZoteroExport(pdf)
            with self.assertRaisesRegex(LibraryFilesError, "明确选择整个库"):
                await list_zotero_pdf_items(zotero, "")

            items = await list_zotero_pdf_items(
                zotero,
                "",
                library_id="user:0",
                whole_library=True,
            )

        self.assertEqual([item["id"] for item in items], ["GROUPITEM"])
        self.assertEqual(zotero.iter_calls, [("user:0", "")])

    async def test_zotero_pdf_tools_use_explicit_library_and_collection_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "group-paper.pdf"
            pdf.write_bytes(MINIMAL_PDF)
            zotero = _FakeScopedZoteroExport(pdf)
            scope = {"library_id": "group:42", "collection_key": "COLLKEY1"}

            items = await list_zotero_pdf_items(
                zotero,
                "Group Collection",
                **scope,
            )
            self.assertEqual([item["id"] for item in items], ["GROUPITEM"])

            destination = root / "exported"
            exported = await export_zotero_pdfs(
                zotero,
                "Group Collection",
                destination,
                item_ids={"GROUPITEM"},
                **scope,
            )
            self.assertEqual(exported["copied"], 1)
            self.assertTrue((destination / "Jane Doe - Scoped Paper.pdf").is_file())

            renamed = await rename_zotero_pdfs(
                zotero,
                "Group Collection",
                **scope,
            )
            self.assertEqual(renamed["renamed"], 1)
            self.assertEqual(
                zotero.iter_calls,
                [("group:42", "COLLKEY1")] * 3,
            )
            self.assertEqual(
                zotero.export_calls,
                [("GROUPITEM", "group:42")] * 3,
            )
            self.assertEqual(
                zotero.rename_calls,
                [("GROUPATT", "Jane Doe - Scoped Paper.pdf", 7, "group:42")],
            )

    async def test_zotero_rename_restores_file_for_unexpected_failure_or_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, error, expected_error in (
                (
                    "runtime",
                    RuntimeError("transport failed"),
                    LibraryFilesError,
                ),
                ("cancel", asyncio.CancelledError(), asyncio.CancelledError),
            ):
                with self.subTest(error=label):
                    pdf = root / f"{label}.pdf"
                    pdf.write_bytes(MINIMAL_PDF)
                    zotero = _FakeScopedZoteroExport(pdf)
                    zotero.rename_attachment_filename = AsyncMock(side_effect=error)

                    with self.assertRaises(expected_error):
                        await rename_zotero_pdfs(
                            zotero,
                            "Group Collection",
                            library_id="group:42",
                            collection_key="COLLKEY1",
                        )

                    self.assertTrue(pdf.is_file())
                    self.assertFalse((root / "Jane Doe - Scoped Paper.pdf").exists())

    async def test_zotero_rename_reconciles_lost_patch_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(MINIMAL_PDF)
            zotero = _FakeScopedZoteroExport(pdf)
            zotero.rename_attachment_filename = AsyncMock(
                side_effect=RuntimeError("response lost")
            )
            zotero.attachment_filename = AsyncMock(
                return_value="Jane Doe - Scoped Paper.pdf"
            )

            result = await rename_zotero_pdfs(
                zotero,
                "Group Collection",
                library_id="group:42",
                collection_key="COLLKEY1",
            )

            self.assertEqual(result["renamed"], 1)
            self.assertEqual(len(result["warnings"]), 1)
            self.assertFalse(pdf.exists())
            self.assertTrue((root / "Jane Doe - Scoped Paper.pdf").is_file())

    async def test_zotero_rename_preserves_both_names_when_reconciliation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(MINIMAL_PDF)
            zotero = _FakeScopedZoteroExport(pdf)
            zotero.rename_attachment_filename = AsyncMock(
                side_effect=RuntimeError("response lost")
            )
            zotero.attachment_filename = AsyncMock(
                side_effect=RuntimeError("lookup failed")
            )

            with self.assertRaisesRegex(LibraryFilesError, "保留新旧两个文件名"):
                await rename_zotero_pdfs(
                    zotero,
                    "Group Collection",
                    library_id="group:42",
                    collection_key="COLLKEY1",
                )

            self.assertTrue(pdf.is_file())
            self.assertTrue((root / "Jane Doe - Scoped Paper.pdf").is_file())

    async def test_zotero_rename_never_overwrites_reappeared_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(MINIMAL_PDF)
            zotero = _FakeScopedZoteroExport(pdf)

            async def recreate_original_then_fail(*_args, **_kwargs):
                pdf.write_bytes(b"replacement created during PATCH")
                raise RuntimeError("transport failed")

            zotero.rename_attachment_filename = AsyncMock(
                side_effect=recreate_original_then_fail
            )

            with self.assertRaisesRegex(LibraryFilesError, "原路径已重新出现"):
                await rename_zotero_pdfs(
                    zotero,
                    "Group Collection",
                    library_id="group:42",
                    collection_key="COLLKEY1",
                )

            self.assertEqual(pdf.read_bytes(), b"replacement created during PATCH")
            self.assertTrue((root / "Jane Doe - Scoped Paper.pdf").is_file())

    async def test_zotero_rename_reports_prior_success_when_later_item_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdfs = {
                "ITEM1": root / "one.pdf",
                "ITEM2": root / "two.pdf",
            }
            for path in pdfs.values():
                path.write_bytes(MINIMAL_PDF)

            class PartialFailureZotero:
                async def iter_library_items(
                    self, *, library_id: str, collection_key: str
                ):
                    for key in pdfs:
                        yield {"data": {"key": key, "itemType": "journalArticle"}}

                async def export_row(
                    self,
                    key: str,
                    fallback_metadata=None,
                    *,
                    library_id: str = "user:0",
                ):
                    index = 1 if key == "ITEM1" else 2
                    return {
                        "attachment_key": f"ATT{index}",
                        "attachment_version": index,
                        "metadata": {
                            "title": f"Paper {index}",
                            "authors": ["Doe, Jane"],
                        },
                        "pdf_path": pdfs[key],
                    }

                async def rename_attachment_filename(
                    self,
                    attachment_key: str,
                    filename: str,
                    version: int | None,
                    *,
                    library_id: str = "user:0",
                ):
                    if attachment_key == "ATT2":
                        raise RuntimeError("second PATCH failed")

                async def attachment_filename(
                    self, attachment_key: str, *, library_id: str = "user:0"
                ) -> str:
                    return "two.pdf"

            with self.assertRaisesRegex(
                LibraryFilesError,
                "操作部分完成：已成功重命名 1 个文件",
            ):
                await rename_zotero_pdfs(
                    PartialFailureZotero(),
                    "Collection",
                    library_id="group:42",
                    collection_key="COLLKEY1",
                )

            self.assertTrue((root / "Jane Doe - Paper 1.pdf").is_file())
            self.assertTrue(pdfs["ITEM2"].is_file())

    async def test_zotero_rename_restores_file_when_attachment_key_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(MINIMAL_PDF)
            zotero = _FakeScopedZoteroExport(pdf)
            zotero.export_row = AsyncMock(
                return_value={
                    "zotero_key": "GROUPITEM",
                    "attachment_key": None,
                    "attachment_version": None,
                    "metadata": {
                        "title": "Scoped Paper",
                        "authors": ["Doe, Jane"],
                    },
                    "pdf_path": pdf,
                }
            )

            with self.assertRaisesRegex(LibraryFilesError, "缺少标识"):
                await rename_zotero_pdfs(
                    zotero,
                    "Group Collection",
                    library_id="group:42",
                    collection_key="COLLKEY1",
                )

            self.assertTrue(pdf.is_file())
            self.assertFalse((root / "Jane Doe - Scoped Paper.pdf").exists())

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
