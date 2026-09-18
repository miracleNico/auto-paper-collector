from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_endnote.config import Settings
from paper_endnote.db import BatchDeletingError, Database
from paper_endnote.deletion import (
    BatchCleanupError,
    BatchDeletionManager,
    cleanup_batch_files,
)
from paper_endnote.pipeline import PipelineManager
from paper_endnote.user_config import OcrOptions, load_preset


def _settings(root: Path) -> Settings:
    runtime = root / "runtime"
    paths = {
        "downloads": runtime / "downloads",
        "generated": runtime / "generated",
        "backups": runtime / "backups",
        "profile": runtime / "profile",
        "uploads": runtime / "uploads",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_root=root,
        runtime_dir=runtime,
        database_path=runtime / "db.sqlite3",
        download_dir=paths["downloads"],
        generated_dir=paths["generated"],
        backup_dir=paths["backups"],
        browser_profile_dir=paths["profile"],
        endnote_exe=root / "EndNote.exe",
        endnote_library=None,
        crossref_mailto="",
        unpaywall_email="",
        config_path=runtime / "config.toml",
        institution=load_preset("mcgill"),
        ocr=OcrOptions(),
        auto_institution=False,
        auto_commit=False,
    )


def _create_batch(db: Database, name: str = "batch", *, count: int = 2) -> str:
    return db.create_batch(
        name=name,
        target_library=f"{name}-library",
        library_mode="new",
        items=[
            {
                "input_text": f"10.1000/{name}-{index}",
                "doi": f"10.1000/{name}-{index}",
                "title": f"Paper {index}",
            }
            for index in range(count)
        ],
    )


def _deletion_item(db: Database, batch_id: str, *, size: int = 0) -> dict:
    batch = db.get_batch(batch_id)
    assert batch is not None
    return {
        "id": batch_id,
        "name": batch["name"],
        "paper_count": len(batch["papers"]),
        "bytes": size,
        "error": None,
    }


class DatabaseBatchDeletionTests(unittest.TestCase):
    def test_delete_transaction_cascades_and_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "db.sqlite3")
            target_id = _create_batch(db, "target")
            survivor_id = _create_batch(db, "survivor", count=1)
            target = db.get_batch(target_id)
            assert target is not None
            paper_id = target["papers"][0]["id"]
            db.replace_candidates(
                paper_id,
                [{"score": 0.9, "title": "Candidate", "doi": "10.1000/candidate"}],
            )
            db.start_operation(paper_id, "download", {"source": "test"})
            db.event(target_id, "extra event", paper_id=paper_id)
            db.set_setting("keep", "yes")

            deletion_id = db.create_deletion_operation(
                [_deletion_item(db, target_id, size=321)]
            )
            self.assertTrue(db.is_batch_deleting(target_id))
            with self.assertRaises(BatchDeletingError):
                db.update_paper(paper_id, status="queued")

            db.delete_batch_transaction(deletion_id, target_id, deleted_bytes=321)
            self.assertEqual(db.finalize_deletion_operation(deletion_id), "completed")

            self.assertIsNone(db.get_batch(target_id))
            self.assertIsNotNone(db.get_batch(survivor_id))
            self.assertEqual(db.get_setting("keep"), "yes")
            operation = db.get_deletion_operation(deletion_id)
            assert operation is not None
            self.assertEqual(operation["status"], "completed")
            self.assertEqual(operation["batches"][0]["status"], "completed")
            self.assertEqual(operation["batches"][0]["deleted_bytes"], 321)

            with db.connect() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM papers WHERE batch_id=?", (target_id,)
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM candidates WHERE paper_id=?", (paper_id,)
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM operations WHERE paper_id=?", (paper_id,)
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM events WHERE batch_id=?", (target_id,)
                    ).fetchone()[0],
                    0,
                )

    def test_delete_transaction_rolls_back_if_history_update_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "db.sqlite3")
            batch_id = _create_batch(db, "rollback", count=1)
            deletion_id = db.create_deletion_operation([_deletion_item(db, batch_id)])
            with db.connect() as connection:
                connection.execute(
                    """CREATE TRIGGER reject_deletion_history_update
                    BEFORE UPDATE ON deletion_batches
                    BEGIN SELECT RAISE(ABORT, 'injected history failure'); END"""
                )

            with self.assertRaises(sqlite3.IntegrityError):
                db.delete_batch_transaction(deletion_id, batch_id, deleted_bytes=1)

            self.assertIsNotNone(db.get_batch(batch_id))
            operation = db.get_deletion_operation(deletion_id)
            assert operation is not None
            self.assertEqual(operation["batches"][0]["status"], "pending")

    def test_recoverable_operation_survives_database_reopen_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            db = Database(path)
            batch_id = _create_batch(db, "recover", count=1)
            first_id = db.create_deletion_operation([_deletion_item(db, batch_id)])
            db.set_deletion_operation_status(first_id, "running")
            db.set_deletion_batch_status(first_id, batch_id, "deleting")

            reopened = Database(path)
            recoverable = reopened.list_recoverable_deletions()
            self.assertEqual([item["id"] for item in recoverable], [first_id])
            self.assertEqual(recoverable[0]["batches"][0]["status"], "deleting")

            reopened.set_deletion_batch_status(
                first_id, batch_id, "failed", error="file is locked"
            )
            self.assertEqual(
                reopened.finalize_deletion_operation(first_id), "partial_failed"
            )
            self.assertEqual(reopened.list_recoverable_deletions(), [])

            second_id = reopened.create_deletion_operation(
                [_deletion_item(reopened, batch_id)]
            )
            self.assertNotEqual(first_id, second_id)
            self.assertEqual(
                reopened.get_deletion_operation(second_id)["batches"][0]["status"],
                "pending",
            )


class BatchFileCleanupTests(unittest.TestCase):
    def test_cleanup_is_idempotent_and_limited_to_per_paper_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            paper_id = "paper-1"
            download = settings.download_dir / paper_id / "paper.pdf"
            upload = settings.runtime_dir / "uploads" / paper_id / "upload.part"
            download.parent.mkdir(parents=True)
            upload.parent.mkdir(parents=True)
            download.write_bytes(b"download")
            upload.write_bytes(b"upload")
            exported = root / "exported" / "keep.pdf"
            zotero = root / "zotero-storage" / "keep.pdf"
            exported.parent.mkdir()
            zotero.parent.mkdir()
            exported.write_bytes(b"export")
            zotero.write_bytes(b"zotero")

            self.assertEqual(
                cleanup_batch_files(settings, [paper_id]),
                len(b"download") + len(b"upload"),
            )
            self.assertFalse(download.parent.exists())
            self.assertFalse(upload.parent.exists())
            self.assertTrue(exported.is_file())
            self.assertTrue(zotero.is_file())
            self.assertEqual(cleanup_batch_files(settings, [paper_id]), 0)

    def test_cleanup_rejects_traversal_without_touching_outside_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            outside = root / "outside"
            outside.mkdir()
            keep = outside / "keep.pdf"
            keep.write_bytes(b"keep")

            with self.assertRaises(BatchCleanupError):
                cleanup_batch_files(settings, [".."])
            self.assertTrue(keep.is_file())

    def test_cleanup_rejects_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            outside = root / "outside"
            outside.mkdir()
            keep = outside / "keep.pdf"
            keep.write_bytes(b"keep")
            link = settings.download_dir / "paper-link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                # A skip would silently drop this path-escape guard from CI.
                if os.environ.get("CI"):
                    self.fail(f"directory symlinks must be available in CI: {exc}")
                self.skipTest(f"directory symlinks are unavailable: {exc}")
            with self.assertRaises(BatchCleanupError):
                cleanup_batch_files(settings, ["paper-link"])
            self.assertTrue(keep.is_file())

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_cleanup_rejects_windows_junction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            outside = root / "outside"
            outside.mkdir()
            keep = outside / "keep.pdf"
            keep.write_bytes(b"keep")
            junction = settings.download_dir / "paper-junction"
            created = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(f"junction creation is unavailable: {created.stderr}")
            try:
                with self.assertRaises(BatchCleanupError):
                    cleanup_batch_files(settings, ["paper-junction"])
                self.assertTrue(keep.is_file())
            finally:
                if junction.exists():
                    os.rmdir(junction)


class _FakePipeline:
    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.fail_batches: set[str] = set()

    def is_batch_running(self, batch_id: str) -> bool:
        return False

    async def prepare_batch_deletion(
        self, batch_id: str, paper_ids: list[str]
    ) -> None:
        self.prepared.append(batch_id)
        if batch_id in self.fail_batches:
            raise RuntimeError("injected stop failure")


class _QuietClient:
    async def close(self) -> None:
        return None


class _TrackedBrowser:
    def __init__(self) -> None:
        self.stopped: list[list[str]] = []

    async def stop_papers(self, paper_ids: list[str]) -> None:
        self.stopped.append(list(paper_ids))

    async def close(self) -> None:
        return None


class _BlockingZotero:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ensure_collection(self, name: str, *, create: bool) -> str:
        self.started.set()
        await self.release.wait()
        return "COLLECTION"

    async def commit_paper(
        self, collection_key: str, metadata: dict, pdf_path: Path | None
    ) -> dict:
        return {"record_number": "ITEM", "existing_full_text": True}

    async def close(self) -> None:
        return None


async def _real_pipeline(
    settings: Settings, db: Database
) -> tuple[PipelineManager, _TrackedBrowser, _BlockingZotero]:
    manager = PipelineManager(settings, db)
    await manager.crossref.close()
    await manager.unpaywall.close()
    await manager.zotero.close()
    browser = _TrackedBrowser()
    zotero = _BlockingZotero()
    manager.crossref = _QuietClient()
    manager.unpaywall = _QuietClient()
    manager.browser = browser
    manager.zotero = zotero
    return manager, browser, zotero


async def _wait_for_terminal(
    manager: BatchDeletionManager, operation_id: str
) -> dict:
    for _ in range(200):
        operation = manager.get(operation_id)
        assert operation is not None
        if operation["status"] in {"completed", "partial_failed"}:
            return operation
        await asyncio.sleep(0.01)
    raise AssertionError(f"deletion {operation_id} did not finish")


class BatchDeletionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_deduplicates_ids_and_missing_batch_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            db = Database(settings.database_path)
            manager = BatchDeletionManager(settings, db, _FakePipeline())

            operation = manager.request(["missing-id", "missing-id"])
            completed = await _wait_for_terminal(manager, operation["id"])

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(len(completed["batches"]), 1)
            self.assertEqual(completed["batches"][0]["status"], "missing")

    async def test_successful_delete_keeps_external_pdf_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            db = Database(settings.database_path)
            pipeline = _FakePipeline()
            manager = BatchDeletionManager(settings, db, pipeline)
            batch_id = _create_batch(db, "external", count=1)
            batch = db.get_batch(batch_id)
            assert batch is not None
            paper_id = batch["papers"][0]["id"]
            internal = settings.download_dir / paper_id / "internal.pdf"
            internal.parent.mkdir()
            internal.write_bytes(b"internal")
            external_pdf = root / "outside.pdf"
            export_dir = root / "export"
            external_pdf.write_bytes(b"external")
            export_dir.mkdir()
            (export_dir / "records.xml").write_text("keep", encoding="utf-8")
            db.update_paper(paper_id, pdf_path=str(external_pdf))
            db.update_batch(batch_id, endnote_export_path=str(export_dir))

            operation = manager.request([batch_id, batch_id])
            completed = await _wait_for_terminal(manager, operation["id"])

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(len(completed["batches"]), 1)
            self.assertEqual(pipeline.prepared, [batch_id])
            self.assertIsNone(db.get_batch(batch_id))
            self.assertFalse(internal.parent.exists())
            self.assertTrue(external_pdf.is_file())
            self.assertTrue((export_dir / "records.xml").is_file())

            repeated = manager.request([batch_id])
            repeated_done = await _wait_for_terminal(manager, repeated["id"])
            self.assertEqual(repeated_done["status"], "completed")
            self.assertEqual(repeated_done["batches"][0]["status"], "missing")

    async def test_one_batch_failure_does_not_block_other_batches_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            db = Database(settings.database_path)
            pipeline = _FakePipeline()
            manager = BatchDeletionManager(settings, db, pipeline)
            failed_id = _create_batch(db, "failed", count=1)
            successful_id = _create_batch(db, "successful", count=1)
            pipeline.fail_batches.add(failed_id)

            operation = manager.request([failed_id, successful_id])
            partial = await _wait_for_terminal(manager, operation["id"])
            statuses = {
                item["batch_id"]: item["status"] for item in partial["batches"]
            }
            self.assertEqual(partial["status"], "partial_failed")
            self.assertEqual(statuses[failed_id], "failed")
            self.assertEqual(statuses[successful_id], "completed")
            self.assertIsNotNone(db.get_batch(failed_id))
            self.assertIsNone(db.get_batch(successful_id))

            pipeline.fail_batches.clear()
            retry = manager.request([failed_id])
            completed = await _wait_for_terminal(manager, retry["id"])
            self.assertEqual(completed["status"], "completed")
            self.assertIsNone(db.get_batch(failed_id))

    async def test_recover_resumes_persisted_running_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            db = Database(settings.database_path)
            batch_id = _create_batch(db, "restart", count=1)
            operation_id = db.create_deletion_operation(
                [_deletion_item(db, batch_id)]
            )
            db.set_deletion_operation_status(operation_id, "running")
            db.set_deletion_batch_status(operation_id, batch_id, "deleting")

            reopened = Database(settings.database_path)
            manager = BatchDeletionManager(settings, reopened, _FakePipeline())
            await manager.recover()
            completed = await _wait_for_terminal(manager, operation_id)

            self.assertEqual(completed["status"], "completed")
            self.assertIsNone(reopened.get_batch(batch_id))

    async def test_cleanup_failure_records_deleted_bytes_and_preserves_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            db = Database(settings.database_path)
            manager = BatchDeletionManager(settings, db, _FakePipeline())
            batch_id = _create_batch(db, "locked", count=1)

            with patch(
                "paper_endnote.deletion.cleanup_batch_files",
                side_effect=BatchCleanupError("file is locked", deleted_bytes=17),
            ):
                operation = manager.request([batch_id])
                failed = await _wait_for_terminal(manager, operation["id"])

            self.assertEqual(failed["status"], "partial_failed")
            self.assertEqual(failed["batches"][0]["status"], "failed")
            self.assertEqual(failed["batches"][0]["deleted_bytes"], 17)
            self.assertIn("locked", failed["batches"][0]["error"])
            self.assertIsNotNone(db.get_batch(batch_id))

    async def test_delete_waits_for_in_flight_zotero_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            db = Database(settings.database_path)
            pipeline, browser, zotero = await _real_pipeline(settings, db)
            manager = BatchDeletionManager(settings, db, pipeline)
            batch_id = _create_batch(db, "commit", count=1)
            batch = db.get_batch(batch_id)
            assert batch is not None
            paper_id = batch["papers"][0]["id"]
            db.update_paper(
                paper_id,
                doi="10.1000/commit",
                title="Commit in progress",
                authors_json=["Doe, Jane"],
                metadata_json={"title": "Commit in progress"},
                metadata_status="verified",
                pdf_status="not_found",
                endnote_status="pending",
                status="endnote_pending",
            )

            pipeline.commit(batch_id)
            await asyncio.wait_for(zotero.started.wait(), timeout=1)
            operation = manager.request([batch_id])
            await asyncio.sleep(0.05)
            in_progress = manager.get(operation["id"])
            assert in_progress is not None
            self.assertEqual(in_progress["status"], "running")
            self.assertEqual(in_progress["batches"][0]["status"], "stopping")
            self.assertIsNotNone(db.get_batch(batch_id))

            zotero.release.set()
            completed = await _wait_for_terminal(manager, operation["id"])
            self.assertEqual(completed["status"], "completed")
            self.assertIsNone(db.get_batch(batch_id))
            self.assertEqual(len(browser.stopped), 2)
            await pipeline.close()

    async def test_delete_drains_upload_like_work_and_blocks_new_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            db = Database(settings.database_path)
            pipeline, _browser, zotero = await _real_pipeline(settings, db)
            zotero.release.set()
            manager = BatchDeletionManager(settings, db, pipeline)
            batch_id = _create_batch(db, "upload", count=1)
            paper = db.get_batch(batch_id)["papers"][0]
            started = asyncio.Event()
            release = asyncio.Event()
            temporary = settings.runtime_dir / "uploads" / paper["id"] / "incoming.pdf"

            async def upload_like_work() -> None:
                async with pipeline.track_batch_work(batch_id, cancellable=False):
                    temporary.parent.mkdir(parents=True, exist_ok=True)
                    temporary.write_bytes(b"partial")
                    started.set()
                    await release.wait()

            upload_task = asyncio.create_task(upload_like_work())
            await asyncio.wait_for(started.wait(), timeout=1)
            operation = manager.request([batch_id])
            await asyncio.sleep(0.05)
            self.assertFalse(upload_task.done())
            self.assertIsNotNone(db.get_batch(batch_id))
            with self.assertRaises(BatchDeletingError):
                async with pipeline.track_batch_work(batch_id, cancellable=False):
                    pass

            release.set()
            await upload_task
            completed = await _wait_for_terminal(manager, operation["id"])
            self.assertEqual(completed["status"], "completed")
            self.assertIsNone(db.get_batch(batch_id))
            self.assertFalse(temporary.parent.exists())
            await pipeline.close()


if __name__ == "__main__":
    unittest.main()
