from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from paper_endnote.browser import LoginTimeoutError, PublisherBlockedError
from paper_endnote.config import Settings
from paper_endnote.db import Database
from paper_endnote.pdf_validation import PDFValidation
from paper_endnote.pipeline import PipelineManager
from paper_endnote.user_config import OcrOptions, load_preset


class FakeUnpaywall:
    async def best_location(self, doi):
        return None

    async def close(self) -> None:
        return None


class FakeCrossref:
    async def by_doi(self, doi):
        return {"doi": doi, "title": "Example Paper", "year": 2024, "authors": ["Doe, Jane"], "journal": "J"}

    async def close(self) -> None:
        return None


class FakeZotero:
    def __init__(self) -> None:
        self.commits: list[str] = []

    async def ensure_collection(self, name: str, *, create: bool) -> str:
        return "COL1"

    async def commit_paper(self, collection_key: str, metadata: dict, pdf_path: Path | None) -> dict:
        self.commits.append(str(metadata.get("doi") or ""))
        return {"record_number": "KEY1", "existing_full_text": bool(pdf_path)}

    async def export_row(self, item_key: str, fallback_metadata=None) -> dict:
        return {"metadata": fallback_metadata or {}, "pdf_path": None, "attachment_key": item_key}

    async def close(self) -> None:
        return None


class FakeBrowser:
    def __init__(self) -> None:
        self.acquire_calls: list[str] = []
        self.ensure_calls = 0
        self.fail_login = False
        self.fail_publisher = False
        self.callback = None

    def set_download_callback(self, callback) -> None:
        self.callback = callback

    async def ensure_logged_in(self, entry_url: str) -> None:
        self.ensure_calls += 1
        if self.fail_login:
            raise LoginTimeoutError("等待登录超时：请在 Chrome 中完成登录或 2FA")

    async def acquire_for_paper(self, paper_id: str, url: str, *, on_download_started=None) -> dict:
        self.acquire_calls.append(paper_id)
        if self.fail_login:
            raise LoginTimeoutError("等待登录超时：请在 Chrome 中完成登录或 2FA")
        if self.fail_publisher:
            raise PublisherBlockedError("出版社拒绝：HTTP 403")
        if on_download_started:
            on_download_started()
        return {"source_url": url, "path": "institution-main.pdf"}

    async def close(self) -> None:
        return None


def _settings(root: Path) -> Settings:
    runtime = root / "runtime"
    for name in ("downloads", "generated", "backups", "profile"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    return Settings(
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
        unpaywall_email="",
        config_path=runtime / "config.toml",
        institution=load_preset("mcgill"),
        ocr=OcrOptions(),
        acquisition_sources=("open_access", "institution"),
        auto_institution=True,
        auto_commit=True,
        login_wait_seconds=30,
    )


def _verified(path, **kwargs) -> PDFValidation:
    return PDFValidation(
        valid_pdf=True,
        identity="verified",
        role="main",
        doi="10.1000/example",
        title_similarity=1.0,
        page_count=1,
        text_available=True,
        sha256="abc",
        reason="ok",
    )


def _pipeline(root: Path) -> tuple[PipelineManager, Database, FakeBrowser, FakeZotero]:
    settings = _settings(root)
    db = Database(settings.database_path)
    manager = PipelineManager(settings, db)
    browser = FakeBrowser()
    zotero = FakeZotero()
    manager.crossref = FakeCrossref()
    manager.unpaywall = FakeUnpaywall()
    manager.browser = browser
    manager.zotero = zotero
    manager.institution_gap_seconds = 0
    manager._validate_pdf = _verified  # type: ignore[method-assign]
    return manager, db, browser, zotero


class PipelineInstitutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_oa_miss_goes_to_institution_pending_then_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager, db, browser, zotero = _pipeline(root)
            manager.settings.acquisition_sources = ("institution",)

            async def acquire(paper_id: str, *, on_download_started=None) -> dict:
                paper = db.get_paper(paper_id)
                if on_download_started:
                    on_download_started()
                dest = manager.settings.download_dir / paper_id / "institution-main.pdf"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"%PDF-1.4\n%%EOF\n")
                db.update_paper(
                    paper_id,
                    pdf_status="verified",
                    status="endnote_pending",
                    pdf_path=str(dest),
                    endnote_status="pending",
                    needs_action="commit_endnote",
                    error=None,
                )
                browser.acquire_calls.append(paper_id)
                return {"source_url": paper.get("source_url"), "path": str(dest)}

            manager.acquire_institution_pdf = acquire  # type: ignore[method-assign]
            batch_id = db.create_batch(
                name="t",
                target_library="DTN",
                library_mode="new",
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a", "title": "Example Paper"}],
            )
            await manager._run_batch(batch_id)
            batch = db.get_batch(batch_id)
            self.assertEqual(batch["papers"][0]["status"], "complete")
            self.assertEqual(len(browser.acquire_calls), 1)
            self.assertEqual(zotero.commits, ["10.1000/a"])

    async def test_login_timeout_downgrades_remaining_institution_papers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, browser, zotero = _pipeline(Path(directory))
            browser.fail_login = True
            batch_id = db.create_batch(
                name="t",
                target_library="DTN",
                library_mode="new",
                items=[
                    {"input_text": "10.1000/a", "doi": "10.1000/a", "title": "Example Paper"},
                    {"input_text": "10.1000/b", "doi": "10.1000/b", "title": "Example Paper"},
                ],
            )
            await manager._run_batch(batch_id)
            batch = db.get_batch(batch_id)
            statuses = [paper["status"] for paper in batch["papers"]]
            self.assertEqual(statuses, ["needs_pdf", "needs_pdf"])
            self.assertTrue(all("等待登录超时" in (paper["error"] or "") for paper in batch["papers"]))
            self.assertEqual(browser.ensure_calls, 1)
            self.assertEqual(zotero.commits, ["10.1000/a", "10.1000/b"])

    async def test_auto_institution_off_skips_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, browser, zotero = _pipeline(Path(directory))
            manager.settings.auto_institution = False
            manager.settings.auto_commit = False
            batch_id = db.create_batch(
                name="t",
                target_library="DTN",
                library_mode="new",
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a", "title": "Example Paper"}],
            )
            await manager._run_batch(batch_id)
            paper = db.get_batch(batch_id)["papers"][0]
            self.assertEqual(paper["status"], "needs_pdf")
            self.assertEqual(browser.ensure_calls, 0)
            self.assertEqual(zotero.commits, [])

    async def test_publisher_block_goes_to_manual_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, browser, zotero = _pipeline(Path(directory))
            browser.fail_publisher = True
            batch_id = db.create_batch(
                name="t",
                target_library="DTN",
                library_mode="new",
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a", "title": "Example Paper"}],
            )
            await manager._run_batch(batch_id)
            paper = db.get_batch(batch_id)["papers"][0]
            self.assertEqual(paper["status"], "needs_pdf")
            self.assertIn("出版社拒绝", paper["error"] or "")
            self.assertEqual(zotero.commits, ["10.1000/a"])

    async def test_institution_discovery_timeout_goes_to_manual_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _browser, zotero = _pipeline(Path(directory))
            manager.settings.acquisition_sources = ("institution",)
            manager.institution_discovery_timeout_seconds = 0.01

            async def never_starts_download(_paper_id: str, *, on_download_started=None) -> dict:
                await asyncio.sleep(60)
                return {}

            manager.acquire_institution_pdf = never_starts_download  # type: ignore[method-assign]
            batch_id = db.create_batch(
                name="t",
                target_library="DTN",
                library_mode="new",
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a", "title": "Example Paper"}],
            )
            await manager._run_batch(batch_id)
            paper = db.get_batch(batch_id)["papers"][0]
            self.assertEqual(paper["status"], "needs_pdf")
            self.assertEqual(paper["needs_action"], "manual_pdf")
            self.assertIn("PDF 入口发现", paper["error"] or "")
            self.assertIn("0.01 秒上限", paper["error"] or "")
            self.assertEqual(zotero.commits, ["10.1000/a"])

    async def test_download_time_is_excluded_after_download_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, browser, zotero = _pipeline(Path(directory))
            manager.settings.acquisition_sources = ("institution",)
            manager.institution_discovery_timeout_seconds = 0.01

            async def slow_download(paper_id: str, *, on_download_started=None) -> dict:
                paper = db.get_paper(paper_id)
                if on_download_started:
                    on_download_started()
                await asyncio.sleep(0.05)
                dest = manager.settings.download_dir / paper_id / "institution-main.pdf"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"%PDF-1.4\n%%EOF\n")
                db.update_paper(
                    paper_id,
                    pdf_status="verified",
                    status="endnote_pending",
                    pdf_path=str(dest),
                    endnote_status="pending",
                    needs_action="commit_endnote",
                    error=None,
                )
                browser.acquire_calls.append(paper_id)
                return {"source_url": paper.get("source_url"), "path": str(dest)}

            manager.acquire_institution_pdf = slow_download  # type: ignore[method-assign]
            batch_id = db.create_batch(
                name="t",
                target_library="DTN",
                library_mode="new",
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a", "title": "Example Paper"}],
            )
            await manager._run_batch(batch_id)
            paper = db.get_batch(batch_id)["papers"][0]
            self.assertEqual(paper["status"], "complete")
            self.assertEqual(browser.acquire_calls, [paper["id"]])
            self.assertEqual(zotero.commits, ["10.1000/a"])

    async def test_download_timeout_is_not_reported_as_discovery_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _browser, zotero = _pipeline(Path(directory))
            manager.settings.acquisition_sources = ("institution",)
            manager.institution_discovery_timeout_seconds = 0.01

            async def download_times_out(_paper_id: str, *, on_download_started=None) -> dict:
                if on_download_started:
                    on_download_started()
                raise TimeoutError("PDF response body timed out")

            manager.acquire_institution_pdf = download_times_out  # type: ignore[method-assign]
            batch_id = db.create_batch(
                name="t",
                target_library="DTN",
                library_mode="new",
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a", "title": "Example Paper"}],
            )
            await manager._run_batch(batch_id)
            paper = db.get_batch(batch_id)["papers"][0]
            self.assertEqual(paper["status"], "needs_pdf")
            self.assertEqual(paper["error"], "PDF response body timed out")
            self.assertNotIn("PDF 入口发现", paper["error"] or "")
            self.assertEqual(zotero.commits, ["10.1000/a"])


if __name__ == "__main__":
    unittest.main()
