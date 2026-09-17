from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from paper_endnote.browser import BrowserSession
from paper_endnote.publishers import is_publisher_block, publisher_pdf_candidates
from paper_endnote.user_config import (
    InstitutionProfile,
    extract_doi_from_ezproxy_url,
    institution_openurl,
    load_preset,
)


class BrowserSessionTests(unittest.TestCase):
    def test_profile_directories_are_isolated_for_non_ascii_school_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = type(
                "Settings", (), {"browser_profile_dir": Path(directory) / "profile"}
            )()
            session = BrowserSession(settings)
            first = InstitutionProfile(
                id="示例大学一", name="一", access_type="manual_browser"
            )
            second = InstitutionProfile(
                id="示例大学二", name="二", access_type="manual_browser"
            )
            self.assertNotEqual(session._profile_dir(first), session._profile_dir(second))
            self.assertEqual(
                session._profile_dir(load_preset("mcgill")),
                settings.browser_profile_dir,
            )

    def test_extracts_embedded_pdf_from_wrapper(self) -> None:
        markup = '<html><embed src="/ielx8/1/2/article.pdf?token=ok"></html>'
        self.assertEqual(
            BrowserSession._embedded_candidates(markup, "https://publisher.example/stamp.jsp"),
            ["https://publisher.example/ielx8/1/2/article.pdf?token=ok"],
        )

    def test_ignores_javascript_and_supplement_has_lower_rank(self) -> None:
        main = {"text": "Download PDF", "href": "https://example.test/main.pdf"}
        supplement = {"text": "Supplement PDF", "href": "https://example.test/supp.pdf"}
        self.assertGreater(
            BrowserSession._rank_candidate(main), BrowserSession._rank_candidate(supplement)
        )

    def test_builds_worldcat_resolver_url_from_mcgill_doi_entry(self) -> None:
        entry = "https://proxy.library.mcgill.ca/login?url=https://doi.org/10.1016/j.adhoc.2023.103307"
        doi = extract_doi_from_ezproxy_url(entry)
        resolver = institution_openurl(load_preset("mcgill"), doi or "")
        self.assertEqual(
            resolver,
            "https://mcgill.on.worldcat.org/atoztitles/link?"
            "url_ver=Z39.88-2004&rft_id=info%3Adoi%2F10.1016%2Fj.adhoc.2023.103307",
        )

    def test_skips_worldcat_resolver_for_non_doi_targets(self) -> None:
        self.assertIsNone(
            extract_doi_from_ezproxy_url(
                "https://proxy.library.mcgill.ca/login?url=https://publisher.example/article/1"
            )
        )

    def test_ieee_stamp_pdf_from_arnumber(self) -> None:
        candidates = publisher_pdf_candidates(
            "https://ieeexplore-ieee-org.proxy3.library.mcgill.ca/document/6196145"
        )
        hrefs = [item["href"] for item in candidates]
        self.assertEqual(
            hrefs[0],
            "https://ieeexplore-ieee-org.proxy3.library.mcgill.ca/stampPDF/getPDF.jsp?tp=&arnumber=6196145&ref=",
        )
        self.assertIn(
            "https://ieeexplore-ieee-org.proxy3.library.mcgill.ca/stamp/stamp.jsp?tp=&arnumber=6196145",
            hrefs,
        )

    def test_ieee_ielx_from_article_url(self) -> None:
        candidates = publisher_pdf_candidates(
            "https://ieeexplore.ieee.org/ielx7/6287639/9312710/10766816.pdf?tp=&arnumber=10766816"
        )
        hrefs = [item["href"] for item in candidates]
        self.assertTrue(any("stampPDF/getPDF.jsp" in href for href in hrefs))
        self.assertIn(
            "https://ieeexplore.ieee.org/ielx7/6287639/9312710/10766816.pdf",
            hrefs,
        )

    def test_publisher_block_detects_403_and_captcha(self) -> None:
        self.assertTrue(is_publisher_block(403, b"<html>no</html>"))
        self.assertTrue(is_publisher_block(200, b"<html>ip blocked</html>", "ip blocked"))
        self.assertTrue(is_publisher_block(200, b"<html>Please complete captcha</html>", "Please complete captcha"))
        self.assertFalse(is_publisher_block(403, b"%PDF-1.4\n"))
        self.assertFalse(is_publisher_block(200, b"<html>article</html>", "article"))

    def test_detects_ezproxy_login_and_idp_urls(self) -> None:
        session = BrowserSession.__new__(BrowserSession)
        session.settings = type("Settings", (), {"institution": load_preset("mcgill")})()
        self.assertTrue(
            session._is_login_url(
                "https://proxy.library.mcgill.ca/login?url=https://doi.org/10.1109/example"
            )
        )
        self.assertTrue(session._is_login_url("https://login.microsoftonline.com/common/oauth2"))
        self.assertFalse(
            session._is_login_url(
                "https://ieeexplore-ieee-org.proxy3.library.mcgill.ca/document/6196145"
            )
        )

    def test_elsevier_pdfft_from_pii(self) -> None:
        candidates = publisher_pdf_candidates(
            "https://www-sciencedirect-com.proxy3.library.mcgill.ca/science/article/pii/S1570870523001234"
        )
        self.assertIn("/pdfft?isDTMRedir=true&download=true", candidates[0]["href"])
        self.assertIn("S1570870523001234", candidates[0]["href"])


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://publisher.example/article/1"

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    async def title(self) -> str:
        return "Example"

    async def close(self) -> None:
        return None


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    async def new_page(self) -> _FakePage:
        return self.page


class BrowserSessionAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_popup_inherits_its_opener_paper_binding(self) -> None:
        session = BrowserSession.__new__(BrowserSession)
        session._page_papers = {}
        session._pages_by_paper = {}
        session._bound_pages = set()

        class BoundPage:
            def __init__(self, opener=None) -> None:
                self._opener = opener

            def on(self, *_args) -> None:
                return None

            async def opener(self):
                return self._opener

        opener = BoundPage()
        popup = BoundPage(opener)
        session._bind_page(opener, "paper-correct")
        session._bind_page(popup, None)
        await session._inherit_opener_binding(popup)

        self.assertEqual(session._page_papers[id(popup)], "paper-correct")

    async def test_same_named_download_never_overwrites_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = BrowserSession.__new__(BrowserSession)
            session.settings = type(
                "Settings", (), {"download_dir": Path(directory)}
            )()
            session._blocked_papers = set()
            saved: list[Path] = []

            async def callback(_paper_id: str, path: Path) -> None:
                saved.append(path)

            session._download_callback = callback
            destination = Path(directory) / "paper-1" / "manual-paper.pdf"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"verified-original")

            class SameNamedDownload:
                suggested_filename = "paper.pdf"

                async def save_as(self, path: str) -> None:
                    Path(path).write_bytes(b"new-download")

            await session._save_download(SameNamedDownload(), "paper-1")

            self.assertEqual(destination.read_bytes(), b"verified-original")
            self.assertEqual(len(saved), 1)
            self.assertNotEqual(saved[0], destination)
            self.assertEqual(saved[0].read_bytes(), b"new-download")

    async def test_cancelled_download_wait_removes_partial_browser_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = BrowserSession.__new__(BrowserSession)
            session.settings = type(
                "Settings", (), {"download_dir": Path(directory)}
            )()
            session._blocked_papers = set()
            session._download_callback = None
            session._download_tasks = {}
            started = asyncio.Event()

            class SlowDownload:
                suggested_filename = "late.pdf"

                async def save_as(self, destination: str) -> None:
                    Path(destination).write_bytes(b"partial")
                    started.set()
                    await asyncio.Event().wait()

            download_task = asyncio.create_task(
                session._save_download(SlowDownload(), "paper-1")
            )
            session._download_tasks["paper-1"] = {download_task}
            waiter = asyncio.create_task(session.wait_for_downloads("paper-1"))
            await started.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter

            destination = Path(directory) / "paper-1" / "manual-late.pdf"
            self.assertTrue(download_task.cancelled())
            self.assertFalse(destination.exists())

    async def test_failed_download_save_removes_partial_browser_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = BrowserSession.__new__(BrowserSession)
            session.settings = type(
                "Settings", (), {"download_dir": Path(directory)}
            )()
            session._blocked_papers = set()
            session._download_callback = None

            class FailingDownload:
                suggested_filename = "broken.pdf"

                async def save_as(self, destination: str) -> None:
                    Path(destination).write_bytes(b"partial")
                    raise OSError("download save failed")

            destination = Path(directory) / "paper-1" / "manual-broken.pdf"
            with self.assertRaisesRegex(OSError, "download save failed"):
                await session._save_download(FailingDownload(), "paper-1")

            self.assertFalse(destination.exists())

    async def test_download_start_callback_runs_before_slow_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = BrowserSession.__new__(BrowserSession)
            page = _FakePage()
            session.settings = type(
                "Settings",
                (),
                {
                    "download_dir": Path(directory),
                    "institution": type("Institution", (), {"name": "Test Institution"})(),
                },
            )()
            session._context = _FakeContext(page)
            session._acquire_lock = asyncio.Lock()
            session._active_paper_id = None
            session._blocked_papers = set()
            session._download_callback = None
            session._bind_page = lambda *_args: None

            async def open_entry(_page, url: str, **_kwargs) -> str:
                return url

            async def reveal(_page) -> None:
                return None

            async def candidate_links(_page) -> list[dict[str, str]]:
                return [{"text": "PDF", "href": "https://publisher.example/main.pdf"}]

            events: list[str] = []

            async def slow_fetch(_paper_id, _candidates, destination: Path) -> dict:
                events.append("fetch")
                await asyncio.sleep(0.05)
                return {"source_url": "https://publisher.example/main.pdf", "path": str(destination)}

            session._open_institution_entry = open_entry
            session._reveal = reveal
            session._is_login_url = lambda _url, _profile=None: False
            session._candidate_links = candidate_links
            session._fetch_pdf = slow_fetch

            async with asyncio.timeout(0.01) as discovery_timeout:
                def download_started() -> None:
                    events.append("start")
                    discovery_timeout.reschedule(None)

                result = await session.acquire_for_paper(
                    "paper-1",
                    "https://publisher.example/article/1",
                    on_download_started=download_started,
                )

            self.assertEqual(events, ["start", "fetch"])
            self.assertIn("main.pdf", result["source_url"])


if __name__ == "__main__":
    unittest.main()
