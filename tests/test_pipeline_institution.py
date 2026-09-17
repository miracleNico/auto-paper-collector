from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from paper_endnote.browser import LoginTimeoutError, PublisherBlockedError
from paper_endnote.config import Settings
from paper_endnote.db import Database
from paper_endnote.institution_access import NeedsManualInstitutionAction
from paper_endnote.pdf_validation import PDFValidation, sha256_file
from paper_endnote.pipeline import PipelineManager
from paper_endnote.user_config import InstitutionProfile, OcrOptions, load_preset


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
        self.session_marks: list[tuple[str, str, str]] = []

    def set_download_callback(self, callback) -> None:
        self.callback = callback

    async def ensure_logged_in(self, entry_url: str, *, profile=None, publisher=None) -> None:
        self.ensure_calls += 1
        if self.fail_login:
            raise LoginTimeoutError("等待登录超时：请在 Chrome 中完成登录或 2FA")

    async def acquire_for_paper(
        self,
        paper_id: str,
        url: str,
        *,
        on_download_started=None,
        publisher=None,
        profile=None,
    ) -> dict:
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

    def mark_institution_session(self, publisher, state, *, profile=None) -> None:
        self.session_marks.append((str(profile.id if profile else ""), publisher, str(state)))

    async def continue_institution_access(
        self,
        paper_id,
        target_url=None,
        *,
        publisher=None,
        profile=None,
        on_download_started=None,
    ) -> dict:
        raise NeedsManualInstitutionAction(
            "institution_entry_not_found",
            publisher="generic",
            paper_id=paper_id,
            page_url=target_url,
        )


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
    async def test_generic_result_uses_resolved_publisher_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, _db, _browser, _zotero = _pipeline(Path(directory))
            self.assertEqual(
                manager._resolved_publisher(
                    {"publisher_url": "https://ieeexplore.ieee.org/document/1"},
                    "generic",
                ),
                "ieee",
            )

    async def test_batch_keeps_acquisition_switches_after_global_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _browser, _zotero = _pipeline(Path(directory))
            profile = InstitutionProfile(
                id="oa-school",
                name="OA School",
                access_type="manual_browser",
            )
            batch_id = db.create_batch(
                name="snapshot",
                target_library="Test",
                library_mode="new",
                institution_config={
                    **profile.as_dict(),
                    "_acquisition_sources": ["open_access"],
                    "_auto_institution": False,
                },
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a"}],
            )
            manager.settings.acquisition_sources = ("open_access", "institution")
            manager.settings.auto_institution = True

            self.assertEqual(
                manager._acquisition_options_for_batch(batch_id),
                (("open_access",), False),
            )

    async def test_batch_uses_snapshot_after_global_school_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _browser, _zotero = _pipeline(Path(directory))
            original = InstitutionProfile(
                id="school-a",
                name="School A",
                access_type="manual_browser",
            )
            batch_id = db.create_batch(
                name="snapshot",
                target_library="Test",
                library_mode="new",
                institution_config=original.as_dict(),
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a"}],
            )
            manager.settings.institution = load_preset("mcgill")
            profile = manager._institution_profile_for_batch(batch_id)
            self.assertEqual(profile.id, "school-a")
            self.assertEqual(profile.access_type, "manual_browser")

    async def test_batch_login_uses_snapshot_instead_of_current_school(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, browser, zotero = _pipeline(Path(directory))
            original = InstitutionProfile(
                id="school-a",
                name="School A",
                access_type="manual_browser",
                login_url="https://a.example.edu/login",
            )
            batch_id = db.create_batch(
                name="snapshot",
                target_library="Test",
                library_mode="new",
                institution_config=original.as_dict(),
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a"}],
            )
            manager.settings.institution = InstitutionProfile(
                id="school-b",
                name="School B",
                access_type="manual_browser",
                login_url="https://b.example.edu/login",
            )
            seen: list[str] = []

            async def open_login(target_url, *, publisher=None, profile=None):
                seen.append(profile.id)
                return {
                    "status": "waiting",
                    "publisher": publisher or "generic",
                    "page_url": target_url,
                }

            browser.open_institution_login = open_login  # type: ignore[attr-defined]
            await manager.open_institution_login(batch_id=batch_id)
            self.assertEqual(seen, ["school-a"])

    async def test_access_probe_opens_login_without_pdf_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, _db, browser, _zotero = _pipeline(Path(directory))
            manager.settings.institution = InstitutionProfile(
                id="school-a",
                name="School A",
                access_type="manual_browser",
                login_url="https://login.example.invalid/",
            )
            calls: list[str] = []

            async def open_login(target_url, *, publisher=None, profile=None):
                calls.append(target_url)
                return {
                    "status": "waiting",
                    "publisher": publisher or "generic",
                    "page_url": "https://login.example.invalid/",
                }

            browser.open_institution_login = open_login  # type: ignore[attr-defined]
            result = await manager.test_institution_access(
                target_url="https://publisher.example.invalid/article/1"
            )
            self.assertEqual(result["download_attempted"], False)
            self.assertEqual(len(calls), 1)
            self.assertEqual(browser.acquire_calls, [])

    async def test_manual_takeover_survives_metadata_only_zotero_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, browser, zotero = _pipeline(Path(directory))
            profile = InstitutionProfile(
                id="manual-school",
                name="Manual School",
                access_type="manual_browser",
                login_url="https://login.example.invalid/",
            )
            manager.settings.institution = profile
            manager.settings.acquisition_sources = ("institution",)

            async def needs_user(
                paper_id: str,
                url: str,
                *,
                on_download_started=None,
                publisher=None,
                profile=None,
            ) -> dict:
                raise NeedsManualInstitutionAction(
                    "login_required",
                    publisher="generic",
                    paper_id=paper_id,
                    page_url="https://login.example.invalid/",
                )

            browser.acquire_for_paper = needs_user  # type: ignore[method-assign]
            batch_id = db.create_batch(
                name="manual",
                target_library="Test",
                library_mode="new",
                institution_config=profile.as_dict(),
                items=[
                    {
                        "input_text": "10.1000/manual",
                        "doi": "10.1000/manual",
                        "title": "Example Paper",
                    }
                ],
            )
            await manager._run_batch(batch_id)
            paper = db.get_batch(batch_id)["papers"][0]
            self.assertEqual(paper["status"], "needs_pdf")
            self.assertEqual(paper["needs_action"], "manual_institution")
            self.assertEqual(paper["endnote_status"], "verified")
            self.assertEqual(zotero.commits, ["10.1000/manual"])

    async def test_continue_manual_access_validates_pdf_and_reuses_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, browser, zotero = _pipeline(Path(directory))
            profile = InstitutionProfile(
                id="manual-school",
                name="Manual School",
                access_type="manual_browser",
            )
            batch_id = db.create_batch(
                name="manual",
                target_library="Test",
                library_mode="new",
                institution_config=profile.as_dict(),
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a"}],
            )
            paper_id = db.get_batch(batch_id)["papers"][0]["id"]
            db.update_paper(
                paper_id,
                doi="10.1000/a",
                title="Example Paper",
                metadata_status="verified",
                status="needs_pdf",
                needs_action="manual_institution",
            )

            async def continue_access(
                requested_id,
                target_url=None,
                *,
                publisher=None,
                profile=None,
                on_download_started=None,
            ) -> dict:
                self.assertEqual(requested_id, paper_id)
                if on_download_started:
                    on_download_started()
                path = manager.settings.download_dir / paper_id / "institution-main.pdf"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"%PDF-1.4\n%%EOF\n")
                await manager._browser_downloaded(paper_id, path)
                return {"source_url": "https://publisher.example.invalid/paper", "path": str(path)}

            browser.continue_institution_access = continue_access  # type: ignore[method-assign]
            result = await manager.continue_institution_pdf(paper_id)
            paper = db.get_paper(paper_id)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(paper["pdf_status"], "verified")
            self.assertEqual(paper["needs_action"], "commit_endnote")
            self.assertEqual(db.get_batch(batch_id)["name"], "manual")
            await manager._commit_batch(batch_id)
            await manager._commit_batch(batch_id)
            self.assertEqual(zotero.commits, ["10.1000/a"])
            self.assertEqual(db.get_paper(paper_id)["status"], "complete")

    async def test_verified_manual_download_is_not_downgraded_by_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, browser, _zotero = _pipeline(Path(directory))
            profile = InstitutionProfile(
                id="manual-school",
                name="Manual School",
                access_type="manual_browser",
            )
            batch_id = db.create_batch(
                name="manual",
                target_library="Test",
                library_mode="new",
                institution_config=profile.as_dict(),
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a"}],
            )
            paper_id = db.get_batch(batch_id)["papers"][0]["id"]
            db.update_paper(
                paper_id,
                doi="10.1000/a",
                title="Example Paper",
                metadata_status="verified",
                status="needs_pdf",
                pdf_status="not_found",
                needs_action="manual_institution",
                institution_publisher="ieee",
                institution_state="waiting",
            )
            detached: list[str] = []
            browser.detach_institution_page = detached.append  # type: ignore[attr-defined]
            path = manager.settings.download_dir / paper_id / "manual-paper.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.4\n%%EOF\n")

            await manager._browser_downloaded(paper_id, path)
            db.update_paper(paper_id, pdf_sha256=sha256_file(path))
            result = await manager.continue_institution_pdf(paper_id)
            paper = db.get_paper(paper_id)

            self.assertEqual(result["status"], "verified")
            self.assertEqual(paper["pdf_status"], "verified")
            self.assertEqual(paper["institution_state"], "ready")
            self.assertEqual(detached, [paper_id])
            self.assertIn(("manual-school", "ieee"), manager._institution_ready_sessions)
            self.assertIn(("manual-school", "ieee", "ready"), browser.session_marks)

    async def test_continue_after_restart_reopens_without_claiming_session_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, browser, _zotero = _pipeline(Path(directory))
            profile = InstitutionProfile(
                id="manual-school",
                name="Manual School",
                access_type="manual_browser",
            )
            batch_id = db.create_batch(
                name="manual",
                target_library="Test",
                library_mode="new",
                institution_config=profile.as_dict(),
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a"}],
            )
            paper_id = db.get_batch(batch_id)["papers"][0]["id"]
            db.update_paper(
                paper_id,
                doi="10.1000/a",
                title="Example Paper",
                metadata_status="verified",
                status="needs_pdf",
                needs_action="manual_institution",
            )

            async def reopen(
                requested_id,
                target_url,
                *,
                on_download_started=None,
                publisher=None,
                profile=None,
            ) -> dict:
                raise NeedsManualInstitutionAction(
                    "login_required",
                    publisher="generic",
                    paper_id=requested_id,
                    page_url=target_url,
                )

            browser.acquire_for_paper = reopen  # type: ignore[method-assign]
            result = await manager.continue_institution_pdf(paper_id)
            self.assertEqual(result["status"], "waiting")
            self.assertEqual(db.get_paper(paper_id)["needs_action"], "manual_institution")

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
            self.assertTrue(
                all(paper["needs_action"] == "manual_institution" for paper in batch["papers"])
            )
            self.assertTrue(
                all(paper["institution_state"] == "expired" for paper in batch["papers"])
            )
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
