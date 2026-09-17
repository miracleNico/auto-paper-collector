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
        self._zotero_operation_lock = asyncio.Lock()
        self.zotero_operation_calls = 0

    @asynccontextmanager
    async def track_batch_work(self, batch_id: str, *, cancellable: bool):
        self.work_calls.append((batch_id, cancellable))
        yield

    @asynccontextmanager
    async def zotero_operation_guard(self):
        async with self._zotero_operation_lock:
            self.zotero_operation_calls += 1
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
    async def test_shutdown_endpoint_schedules_graceful_server_exit(self) -> None:
        from paper_endnote import app as app_module

        called = asyncio.Event()
        previous_callback = app_module.app.state.shutdown_callback
        previous_requested = app_module.app.state.shutdown_requested
        app_module.app.state.shutdown_callback = called.set
        app_module.app.state.shutdown_requested = False
        try:
            result = await app_module.shutdown_local_service()
            await asyncio.wait_for(called.wait(), timeout=1)
        finally:
            app_module.app.state.shutdown_callback = previous_callback
            app_module.app.state.shutdown_requested = previous_requested

        self.assertEqual(result["status"], "shutting_down")

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

    async def test_batch_pdf_rename_is_serialized_with_zotero_commits(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            database, batch_id, _, _ = _batch_with_pdf(Path(directory))
            pipeline = _ToolPipeline()
            with (
                patch.object(app_module, "database", database),
                patch.object(app_module, "pipeline", pipeline),
                patch.object(
                    app_module,
                    "rename_batch_pdfs",
                    return_value={"renamed": 1, "skipped": 0},
                ),
            ):
                await app_module.tool_rename_pdfs(
                    app_module.LibraryToolRequest(
                        source="batch",
                        batch_id=batch_id,
                    )
                )

        self.assertEqual(pipeline.zotero_operation_calls, 1)

    async def test_endnote_pdf_tools_use_per_request_library_without_changing_settings(self) -> None:
        from paper_endnote import app as app_module

        configured = Path(r"C:\Libraries\Configured.enl")
        selected = Path(r"D:\Research\Selected.enl")
        settings = type("ToolSettings", (), {"endnote_library": configured})()
        rename_result = {"renamed": 1, "skipped": 0}
        with (
            patch.object(app_module, "settings", settings),
            patch.object(app_module, "rename_endnote_pdfs", return_value=rename_result) as rename,
        ):
            result = await app_module.tool_rename_pdfs(
                app_module.LibraryToolRequest(
                    source="endnote", endnote_library=str(selected)
                )
            )

        self.assertEqual(result, rename_result)
        rename.assert_called_once_with(selected)
        self.assertEqual(settings.endnote_library, configured)

    async def test_endnote_candidates_and_export_share_selected_library(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "Selected.enl"
            destination = root / "export"
            settings = type("ToolSettings", (), {"endnote_library": None})()
            item = {
                "id": "ref:1",
                "title": "Selected",
                "available": True,
                "size": len(MINIMAL_PDF),
            }
            expected = {
                "copied": 1,
                "skipped": 0,
                "destination": str(destination.resolve()),
                "source": str(selected.with_suffix(".Data") / "PDF"),
                "files": [],
            }
            payload = app_module.LibraryToolRequest(
                source="endnote",
                endnote_library=str(selected),
                destination=str(destination),
                item_ids=["ref:1"],
            )
            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "list_endnote_pdf_items", return_value=[item]) as list_items,
                patch.object(app_module, "export_endnote_data_pdfs", return_value=expected) as export,
            ):
                candidates = await app_module.tool_export_pdf_candidates(payload)
                result = await app_module.tool_export_pdfs(payload)

        self.assertEqual(candidates["source_name"], "Selected")
        self.assertEqual(result, expected)
        list_items.assert_called_once_with(selected)
        export.assert_called_once_with(
            selected, destination.resolve(), item_ids={"ref:1"}
        )

    async def test_endnote_tool_keeps_configured_library_as_legacy_fallback(self) -> None:
        from paper_endnote import app as app_module

        configured = Path(r"C:\Libraries\Configured.enl")
        settings = type("ToolSettings", (), {"endnote_library": configured})()
        with (
            patch.object(app_module, "settings", settings),
            patch.object(app_module, "rename_endnote_pdfs", return_value={"renamed": 0}) as rename,
        ):
            await app_module.tool_rename_pdfs(
                app_module.LibraryToolRequest(source="endnote")
            )
        rename.assert_called_once_with(configured)

    async def test_endnote_tool_rejects_relative_per_request_library(self) -> None:
        from fastapi import HTTPException

        from paper_endnote import app as app_module

        with self.assertRaises(HTTPException) as caught:
            await app_module.tool_rename_pdfs(
                app_module.LibraryToolRequest(
                    source="endnote", endnote_library="relative/Library.enl"
                )
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("绝对路径", caught.exception.detail)

    async def test_zotero_pdf_tools_forward_library_and_collection_keys(self) -> None:
        from paper_endnote import app as app_module

        pipeline = _ToolPipeline()
        rename = AsyncMock(return_value={"renamed": 0, "skipped": 0})
        list_items = AsyncMock(return_value=[])
        payload = app_module.LibraryToolRequest(
            source="zotero",
            zotero_library_id="group:42",
            collection="Group Papers",
            collection_key="COLL0042",
        )
        with (
            patch.object(app_module, "pipeline", pipeline),
            patch.object(app_module, "rename_zotero_pdfs", rename),
            patch.object(app_module, "list_zotero_pdf_items", list_items),
        ):
            await app_module.tool_rename_pdfs(payload)
            candidates = await app_module.tool_export_pdf_candidates(payload)

        rename.assert_awaited_once_with(
            pipeline.zotero,
            "Group Papers",
            library_id="group:42",
            collection_key="COLL0042",
            whole_library=False,
        )
        list_items.assert_awaited_once_with(
            pipeline.zotero,
            "Group Papers",
            library_id="group:42",
            collection_key="COLL0042",
            whole_library=False,
        )
        self.assertEqual(candidates["source_name"], "Group Papers")

    async def test_zotero_collection_endpoint_uses_selected_library(self) -> None:
        from paper_endnote import app as app_module

        pipeline = _ToolPipeline()
        pipeline.zotero.normalize_library_id = lambda value: value
        pipeline.zotero.list_collections = AsyncMock(
            return_value=[{"key": "COLL0042", "name": "Group Papers"}]
        )
        with patch.object(app_module, "pipeline", pipeline):
            result = await app_module.tool_zotero_collections("group:42")

        self.assertEqual(result["library_id"], "group:42")
        self.assertEqual(result["collections"][0]["key"], "COLL0042")
        pipeline.zotero.list_collections.assert_awaited_once_with(
            library_id="group:42"
        )

    async def test_tool_sources_keeps_personal_library_when_group_discovery_fails(self) -> None:
        from paper_endnote import app as app_module

        pipeline = _ToolPipeline()
        pipeline.zotero.list_collections = AsyncMock(
            return_value=[{"key": "PERSONAL1", "name": "Personal Papers"}]
        )
        pipeline.zotero.list_libraries = AsyncMock(
            side_effect=RuntimeError("group discovery unavailable")
        )
        settings = type("ToolSettings", (), {"endnote_library": None})()
        database = type("ToolDatabase", (), {"list_batches": lambda self: []})()

        with (
            patch.object(app_module, "pipeline", pipeline),
            patch.object(app_module, "settings", settings),
            patch.object(app_module, "database", database),
        ):
            result = await app_module.tool_sources()

        self.assertEqual(result["collections"][0]["key"], "PERSONAL1")
        self.assertEqual(result["zotero_libraries"][0]["id"], "user:0")
        self.assertIsNone(result["zotero_error"])
        self.assertIn("group discovery unavailable", result["zotero_library_error"])

    async def test_tool_sources_lists_endnote_libraries_beside_configured_library(self) -> None:
        from paper_endnote import app as app_module

        pipeline = _ToolPipeline()
        pipeline.zotero.list_collections = AsyncMock(return_value=[])
        pipeline.zotero.list_libraries = AsyncMock(return_value=[])
        database = type("ToolDatabase", (), {"list_batches": lambda self: []})()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "Configured.enl"
            sibling = root / "Sibling.enl"
            ignored = root / "notes.txt"
            configured.write_bytes(b"")
            sibling.write_bytes(b"")
            ignored.write_text("not a library", encoding="utf-8")
            settings = type("ToolSettings", (), {"endnote_library": configured})()

            with (
                patch.object(app_module, "pipeline", pipeline),
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                result = await app_module.tool_sources()

        self.assertEqual(
            result["endnote_libraries"],
            [str(configured.resolve()), str(sibling.resolve())],
        )

    async def test_native_endnote_picker_returns_path_or_cancel(self) -> None:
        from paper_endnote import app as app_module

        with patch.object(
            app_module, "pick_endnote_library", return_value=r"D:\Papers\Chosen.enl"
        ) as picker:
            result = await app_module.tool_pick_endnote_library(
                app_module.EndNoteLibraryPickerRequest(
                    initial_path=r"D:\Papers\Old.enl"
                )
            )
        self.assertEqual(result, {"path": r"D:\Papers\Chosen.enl", "cancelled": False})
        picker.assert_called_once_with(r"D:\Papers\Old.enl")

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

    async def test_library_tool_lock_serializes_zotero_rename_and_sync(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "Configured.enl"
            settings = type("ToolSettings", (), {"endnote_library": library})()
            pipeline = _ToolPipeline()
            rename_entered = asyncio.Event()
            release_rename = asyncio.Event()
            calls: list[str] = []

            async def rename(*_args, **_kwargs):
                calls.append("rename-start")
                rename_entered.set()
                await release_rename.wait()
                calls.append("rename-end")
                return {"renamed": 0, "skipped": 0}

            async def sync(*_args, **_kwargs):
                calls.append("sync")
                return {"collection": "Imported", "synced": 1}

            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "pipeline", pipeline),
                patch.object(app_module, "rename_zotero_pdfs", side_effect=rename),
                patch.object(app_module, "sync_endnote_to_zotero", side_effect=sync),
            ):
                rename_task = asyncio.create_task(
                    app_module.tool_rename_pdfs(
                        app_module.LibraryToolRequest(
                            source="zotero",
                            collection="Selected",
                        )
                    )
                )
                await asyncio.wait_for(rename_entered.wait(), timeout=1)
                sync_task = asyncio.create_task(
                    app_module.tool_sync_endnote_zotero(
                        app_module.EndNoteZoteroSyncRequest(collection="Imported")
                    )
                )
                await asyncio.sleep(0)
                self.assertEqual(calls, ["rename-start"])
                release_rename.set()
                await asyncio.gather(rename_task, sync_task)

        self.assertEqual(calls, ["rename-start", "rename-end", "sync"])


if __name__ == "__main__":
    unittest.main()
