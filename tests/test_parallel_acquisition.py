from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from paper_endnote.config import Settings
from paper_endnote.db import Database
from paper_endnote.pdf_validation import PDFValidation
from paper_endnote.pipeline import PipelineManager
from paper_endnote.user_config import OcrOptions, load_preset


def _manager(root: Path) -> tuple[PipelineManager, Database, str, str]:
    runtime = root / "runtime"
    for name in ("downloads", "generated", "backups", "profile"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    settings = Settings(
        app_root=root,
        runtime_dir=runtime,
        database_path=runtime / "db.sqlite3",
        download_dir=runtime / "downloads",
        generated_dir=runtime / "generated",
        backup_dir=runtime / "backups",
        browser_profile_dir=runtime / "profile",
        endnote_exe=root / "EndNote.exe",
        endnote_library=None,
        crossref_mailto="",
        unpaywall_email="test@example.invalid",
        config_path=runtime / "config.toml",
        acquisition_sources=("open_access", "institution"),
        ocr=OcrOptions(),
        institution=load_preset("mcgill"),
        auto_institution=True,
        auto_commit=False,
        login_wait_seconds=30,
    )
    db = Database(settings.database_path)
    manager = PipelineManager(settings, db)
    manager.institution_gap_seconds = 0
    batch_id = db.create_batch(
        name="parallel",
        target_library="Test",
        library_mode="new",
        items=[{"input_text": "10.1000/a", "doi": "10.1000/a", "title": "Paper"}],
    )
    paper_id = db.get_batch(batch_id)["papers"][0]["id"]
    db.update_paper(
        paper_id,
        doi="10.1000/a",
        title="Paper",
        metadata_status="verified",
        status="ready",
    )
    return manager, db, batch_id, paper_id


def _success(source: str, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return {
        "success": True,
        "source": source,
        "source_url": f"https://example.invalid/{source}",
        "version": "published",
        "path": path,
        "sha256": source,
    }


def _review(source: str, path: Path, *, role: str = "main", score: float = 0.5) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return {
        "success": False,
        "review": True,
        "source": source,
        "source_url": f"https://example.invalid/{source}",
        "version": "published",
        "path": path,
        "sha256": f"review-{source}",
        "role": role,
        "title_similarity": score,
        "error": "identity needs review",
    }


class ParallelAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_oa_wins_and_cancels_institution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, batch_id, paper_id = _manager(Path(directory))
            cancelled = asyncio.Event()

            async def oa(_paper):
                return _success(
                    "open_access", manager.settings.download_dir / paper_id / "open-access-main.pdf"
                )

            async def institution(_batch_id, _paper):
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            await manager._find_pdf(batch_id, db.get_paper(paper_id))

            paper = db.get_paper(paper_id)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(paper["pdf_status"], "verified")
            self.assertIn("open_access", paper["source_url"])

    async def test_institution_wins_and_cancels_oa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, batch_id, paper_id = _manager(Path(directory))
            cancelled = asyncio.Event()

            async def oa(_paper):
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            async def institution(_batch_id, _paper):
                return _success(
                    "institution", manager.settings.download_dir / paper_id / "institution-main.pdf"
                )

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            await manager._find_pdf(batch_id, db.get_paper(paper_id))

            paper = db.get_paper(paper_id)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(paper["pdf_status"], "verified")
            self.assertIn("institution", paper["source_url"])

    async def test_one_failure_does_not_cancel_later_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, batch_id, paper_id = _manager(Path(directory))
            institution_finished = asyncio.Event()

            async def oa(_paper):
                return {"success": False, "source": "open_access", "error": "OA miss"}

            async def institution(_batch_id, _paper):
                await asyncio.sleep(0.01)
                institution_finished.set()
                return _success(
                    "institution", manager.settings.download_dir / paper_id / "institution-main.pdf"
                )

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            await manager._find_pdf(batch_id, db.get_paper(paper_id))

            self.assertTrue(institution_finished.is_set())
            self.assertEqual(db.get_paper(paper_id)["pdf_status"], "verified")

    async def test_both_fail_goes_to_manual_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, batch_id, paper_id = _manager(Path(directory))
            stale = manager.settings.download_dir / paper_id / "open-access-main.pdf"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_bytes(b"%PDF-1.4\nstale\n%%EOF\n")
            db.update_paper(
                paper_id,
                pdf_status="needs_review",
                pdf_path=str(stale),
                pdf_sha256="stale-sha",
                version="published",
            )

            async def oa(_paper):
                return {"success": False, "source": "open_access", "error": "OA miss"}

            async def institution(_batch_id, _paper):
                return {"success": False, "source": "institution", "error": "institution miss"}

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            await manager._find_pdf(batch_id, db.get_paper(paper_id))

            paper = db.get_paper(paper_id)
            self.assertEqual(paper["status"], "needs_pdf")
            self.assertEqual(paper["needs_action"], "manual_pdf")
            self.assertIn("OA miss", paper["error"])
            self.assertIn("institution miss", paper["error"])
            self.assertIsNone(paper["pdf_path"])
            self.assertIsNone(paper["pdf_sha256"])
            self.assertIsNone(paper["version"])
            self.assertFalse(stale.exists())

    async def test_best_review_candidate_is_preserved_when_neither_path_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, batch_id, paper_id = _manager(Path(directory))
            oa_path = manager.settings.download_dir / paper_id / "open-access-main.pdf"
            institution_path = manager.settings.download_dir / paper_id / "institution-main.pdf"

            async def oa(_paper):
                return _review("open_access", oa_path, role="main", score=0.8)

            async def institution(_batch_id, _paper):
                return _review("institution", institution_path, role="supplement", score=0.9)

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            await manager._find_pdf(batch_id, db.get_paper(paper_id))

            paper = db.get_paper(paper_id)
            self.assertEqual(paper["status"], "needs_pdf_review")
            self.assertEqual(paper["pdf_status"], "needs_review")
            self.assertEqual(paper["needs_action"], "confirm_pdf")
            self.assertEqual(Path(paper["pdf_path"]), oa_path)
            self.assertTrue(oa_path.exists())
            self.assertFalse(institution_path.exists())

    async def test_browser_download_during_race_is_validated_and_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, batch_id, paper_id = _manager(Path(directory))
            manual = manager.settings.download_dir / paper_id / "manual-paper.pdf"

            async def oa(_paper):
                manual.parent.mkdir(parents=True, exist_ok=True)
                manual.write_bytes(b"%PDF-1.4\nmanual\n%%EOF\n")
                await manager._browser_downloaded(paper_id, manual)
                return {"success": False, "source": "open_access", "error": "OA miss"}

            async def institution(_batch_id, _paper):
                await asyncio.sleep(0.01)
                return {"success": False, "source": "institution", "error": "institution miss"}

            manager._validate_pdf = lambda *_args, **_kwargs: PDFValidation(
                True,
                "verified",
                "main",
                "10.1000/a",
                1.0,
                1,
                True,
                "manual-sha",
                "PDF DOI 与目标一致",
            )
            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            await manager._find_pdf(batch_id, db.get_paper(paper_id))

            paper = db.get_paper(paper_id)
            self.assertEqual(paper["pdf_status"], "verified")
            self.assertEqual(Path(paper["pdf_path"]), manual)
            self.assertTrue(manual.exists())

    async def test_browser_download_during_post_race_gap_is_not_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, batch_id, paper_id = _manager(Path(directory))
            manager.institution_gap_seconds = 0.08
            manual = manager.settings.download_dir / paper_id / "manual-gap.pdf"

            async def oa(_paper):
                return {"success": False, "source": "open_access", "error": "OA miss"}

            async def institution(_batch_id, _paper):
                return {"success": False, "source": "institution", "error": "institution miss"}

            async def late_download():
                await asyncio.sleep(0.02)
                manual.parent.mkdir(parents=True, exist_ok=True)
                manual.write_bytes(b"%PDF-1.4\nmanual\n%%EOF\n")
                await manager._browser_downloaded(paper_id, manual)

            manager._validate_pdf = lambda *_args, **_kwargs: PDFValidation(
                True,
                "verified",
                "main",
                "10.1000/a",
                1.0,
                1,
                True,
                "gap-sha",
                "PDF DOI 与目标一致",
            )
            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            late_task = asyncio.create_task(late_download())
            await manager._find_pdf(batch_id, db.get_paper(paper_id))
            await late_task

            paper = db.get_paper(paper_id)
            self.assertEqual(paper["pdf_status"], "verified")
            self.assertEqual(Path(paper["pdf_path"]), manual)
            self.assertTrue(manual.exists())

    async def test_cancelled_loser_partial_files_are_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, batch_id, paper_id = _manager(Path(directory))
            partial = manager.settings.download_dir / paper_id / "open-access-main.pdf.part"

            async def oa(_paper):
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(b"partial")
                await asyncio.sleep(60)

            async def institution(_batch_id, _paper):
                await asyncio.sleep(0.01)
                return _success(
                    "institution", manager.settings.download_dir / paper_id / "institution-main.pdf"
                )

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            await manager._find_pdf(batch_id, db.get_paper(paper_id))

            self.assertFalse(partial.exists())
            self.assertTrue(Path(db.get_paper(paper_id)["pdf_path"]).exists())

    async def test_institution_timeout_cleans_staged_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, batch_id, paper_id = _manager(Path(directory))
            destination = manager.settings.download_dir / paper_id / "institution-main.pdf"
            manager._institution_session_ready = True

            async def acquire(_paper_id, _target, *, on_download_started=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-1.4\npartial\n%%EOF\n")
                if on_download_started:
                    on_download_started()
                raise TimeoutError("transfer timed out")

            manager.browser.acquire_for_paper = acquire  # type: ignore[method-assign]
            outcome = await manager._try_institution_pdf(
                batch_id, db.get_paper(paper_id)
            )

            self.assertFalse(outcome["success"])
            self.assertIn("transfer timed out", outcome["error"])
            self.assertFalse(destination.exists())

    async def test_late_browser_callback_cannot_overwrite_oa_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, batch_id, paper_id = _manager(Path(directory))

            async def oa(_paper):
                return _success(
                    "open_access", manager.settings.download_dir / paper_id / "open-access-main.pdf"
                )

            async def institution(_batch_id, _paper):
                await asyncio.sleep(60)

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            await manager._find_pdf(batch_id, db.get_paper(paper_id))
            winner = dict(db.get_paper(paper_id))

            late = manager.settings.download_dir / paper_id / "manual-late.pdf"
            late.write_bytes(b"%PDF-1.4\nlate\n%%EOF\n")
            await manager._browser_downloaded(paper_id, late)

            paper = db.get_paper(paper_id)
            self.assertEqual(paper["pdf_path"], winner["pdf_path"])
            self.assertEqual(paper["source_url"], winner["source_url"])
            self.assertFalse(late.exists())


if __name__ == "__main__":
    unittest.main()
