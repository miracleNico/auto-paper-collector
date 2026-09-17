from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from paper_endnote.db import Database
from paper_endnote.library_files import LibraryFilesError


MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class _ToolPipeline:
    def __init__(self) -> None:
        self.zotero = _GuardedZotero()
        self.work_calls: list[tuple[str, bool]] = []

    @asynccontextmanager
    async def track_batch_work(self, batch_id: str, *, cancellable: bool):
        self.work_calls.append((batch_id, cancellable))
        yield


class _GuardedZotero:
    def __init__(self) -> None:
        self._sync_lock = asyncio.Lock()

    @asynccontextmanager
    async def sync_guard(self):
        async with self._sync_lock:
            yield


def _batch_with_pdf(root: Path) -> tuple[Database, str, str, Path]:
    database = Database(root / "db.sqlite3")
    batch_id = database.create_batch(
        name="API export",
        target_library="Selected PDFs",
        library_mode="new",
        items=[
            {
                "input_text": "10.1000/selected",
                "doi": "10.1000/selected",
                "title": "Selected Paper",
            }
        ],
    )
    paper_id = database.get_batch(batch_id)["papers"][0]["id"]
    pdf_path = root / "downloads" / paper_id / "main.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(MINIMAL_PDF)
    database.update_paper(
        paper_id,
        doi="10.1000/selected",
        title="Selected Paper",
        year=2025,
        authors_json=["Doe, Jane"],
        pdf_path=str(pdf_path),
        pdf_status="verified",
    )
    return database, batch_id, paper_id, pdf_path


class ToolApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_library_tool_request_distinguishes_omitted_and_empty_item_ids(self) -> None:
        from paper_endnote import app as app_module

        omitted = app_module.LibraryToolRequest(source="batch", batch_id="batch-id")
        self.assertIsNone(omitted.item_ids)
        self.assertIsNone(app_module.selected_tool_item_ids(omitted))

        explicit_empty = app_module.LibraryToolRequest(
            source="batch", batch_id="batch-id", item_ids=[]
        )
        with self.assertRaisesRegex(LibraryFilesError, "至少选择一篇论文"):
            app_module.selected_tool_item_ids(explicit_empty)

    async def test_batch_pdf_candidates_do_not_expose_pdf_path(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            database, batch_id, paper_id, pdf_path = _batch_with_pdf(Path(directory))
            payload = app_module.LibraryToolRequest(source="batch", batch_id=batch_id)

            with patch.object(app_module, "database", database):
                result = await app_module.tool_export_pdf_candidates(payload)

        self.assertEqual(result["source"], "batch")
        self.assertEqual(result["available_count"], 1)
        self.assertEqual(result["items"][0]["id"], paper_id)
        self.assertNotIn("pdf_path", result["items"][0])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("pdf_path", serialized)
        self.assertNotIn(str(pdf_path), serialized)

    async def test_missing_batch_candidate_keeps_not_found_status(self) -> None:
        from fastapi import HTTPException

        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "empty.sqlite3")
            payload = app_module.LibraryToolRequest(source="batch", batch_id="missing")
            with patch.object(app_module, "database", database):
                with self.assertRaises(HTTPException) as caught:
                    await app_module.tool_export_pdf_candidates(payload)

        self.assertEqual(caught.exception.status_code, 404)

    async def test_batch_pdf_export_passes_selected_ids_to_exporter(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, batch_id, paper_id, _ = _batch_with_pdf(root)
            destination = root / "exports"
            pipeline = _ToolPipeline()
            payload = app_module.LibraryToolRequest(
                source="batch",
                batch_id=batch_id,
                destination=str(destination),
                item_ids=[paper_id],
            )
            expected = {
                "copied": 1,
                "skipped": 0,
                "selected": 1,
                "files": [],
                "destination": str(destination.resolve()),
                "source": "API export",
            }

            with (
                patch.object(app_module, "database", database),
                patch.object(app_module, "pipeline", pipeline),
                patch.object(app_module, "export_batch_pdfs", return_value=expected) as export,
            ):
                result = await app_module.tool_export_pdfs(payload)

        self.assertEqual(result, expected)
        export.assert_called_once_with(
            database,
            batch_id,
            destination.resolve(),
            item_ids={paper_id},
        )
        self.assertEqual(pipeline.work_calls, [(batch_id, False)])

    async def test_endnote_zotero_sync_uses_configured_library_and_collection(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "Configured.enl"
            settings = type("ToolSettings", (), {"endnote_library": library})()
            pipeline = _ToolPipeline()
            sync = AsyncMock(return_value={"collection": "Imported Papers", "synced": 2})
            payload = app_module.EndNoteZoteroSyncRequest(collection="  Imported Papers  ")

            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "pipeline", pipeline),
                patch.object(app_module, "sync_endnote_to_zotero", sync),
            ):
                result = await app_module.tool_sync_endnote_zotero(payload)

        self.assertEqual(result, {"collection": "Imported Papers", "synced": 2})
        sync.assert_awaited_once_with(pipeline.zotero, library, "Imported Papers")

    async def test_endnote_zotero_sync_serializes_concurrent_api_requests(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "Configured.enl"
            settings = type("ToolSettings", (), {"endnote_library": library})()
            pipeline = _ToolPipeline()
            first_entered = asyncio.Event()
            release_first = asyncio.Event()
            entered: list[str] = []
            active = 0
            max_active = 0

            async def sync(_zotero, _library, collection):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                entered.append(collection)
                if collection == "First":
                    first_entered.set()
                    await release_first.wait()
                await asyncio.sleep(0)
                active -= 1
                return {"collection": collection, "synced": 1}

            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "pipeline", pipeline),
                patch.object(app_module, "sync_endnote_to_zotero", side_effect=sync),
            ):
                first = asyncio.create_task(
                    app_module.tool_sync_endnote_zotero(
                        app_module.EndNoteZoteroSyncRequest(collection="First")
                    )
                )
                await asyncio.wait_for(first_entered.wait(), timeout=1)
                second = asyncio.create_task(
                    app_module.tool_sync_endnote_zotero(
                        app_module.EndNoteZoteroSyncRequest(collection="Second")
                    )
                )
                await asyncio.sleep(0)
                self.assertEqual(entered, ["First"])
                release_first.set()
                results = await asyncio.gather(first, second)

        self.assertEqual(max_active, 1)
        self.assertEqual(entered, ["First", "Second"])
        self.assertEqual(
            results,
            [
                {"collection": "First", "synced": 1},
                {"collection": "Second", "synced": 1},
            ],
        )


if __name__ == "__main__":
    unittest.main()
