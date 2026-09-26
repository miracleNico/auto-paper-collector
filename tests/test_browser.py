from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from paper_endnote.browser import BrowserSession
from paper_endnote.publishers import is_publisher_block, publisher_pdf_candidates
from paper_endnote.user_config import (
    InstitutionProfile,
    extract_doi_from_ezproxy_url,
    institution_openurl,
    load_preset,
)


class BrowserSessionTests(unittest.TestCase):
    def test_configured_proxy_hosts_map_to_publisher_without_substring_guessing(self) -> None:
        profile = InstitutionProfile(
            id="example", name="Example", ezproxy_hosts=("proxy.example.edu",)
        )
        session = BrowserSession(SimpleNamespace(institution=profile))
        self.assertEqual(
            session.publisher_for_url(
                "https://ieeexplore-ieee-org.proxy.example.edu/document/1"
            ),
            "ieee",
        )
        self.assertEqual(
            session.publisher_for_url(
                "https://www-sciencedirect-com.proxy.example.edu/science/article/1"
            ),
            "sciencedirect",
        )
        self.assertNotEqual(
            session.publisher_for_url("https://ieeexplore-ieee-org.evil.example/document/1"),
            "ieee",
        )

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
    async def test_retry_invalidation_closes_old_article_tab(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = BrowserSession(SimpleNamespace(download_dir=Path(directory)))

            class Page:
                def __init__(self):
                    self.handlers = {}
                    self.closed = False

                def on(self, event, handler):
                    self.handlers[event] = handler

                async def close(self):
                    self.closed = True

            page = Page()
            session._bind_page(page, "paper-retry")
            session._institution_pages["paper-retry"] = page
            session._institution_targets["paper-retry"] = "https://example.invalid/old"
            await session.invalidate_paper_downloads(
                "paper-retry", close_pages=True
            )
            self.assertTrue(page.closed)
            self.assertNotIn("paper-retry", session._institution_pages)
            self.assertIsNone(session._page_papers[id(page)])

    async def test_invalidate_old_pages_drains_callbacks_and_allows_new_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = BrowserSession(SimpleNamespace(download_dir=Path(directory)))
            save_started = asyncio.Event()
            opener_ready = asyncio.Event()
            delivered: list[str] = []

            async def callback(paper_id, _path):
                delivered.append(paper_id)

            session.set_download_callback(callback)

            class Page:
                def __init__(self, opener=None, wait_for_opener=False):
                    self.handlers = {}
                    self._opener = opener
                    self.wait_for_opener = wait_for_opener

                def on(self, event, handler):
                    self.handlers[event] = handler

                async def opener(self):
                    if self.wait_for_opener:
                        await opener_ready.wait()
                    return self._opener

            class SlowDownload:
                suggested_filename = "old.pdf"

                async def save_as(self, path):
                    Path(path).write_bytes(b"partial")
                    save_started.set()
                    await asyncio.Event().wait()

            class ImmediateDownload:
                suggested_filename = "new.pdf"

                async def save_as(self, path):
                    Path(path).write_bytes(b"%PDF-1.4\n")

            old_page = Page()
            old_popup = Page(old_page)
            pending_popup = Page(old_page, wait_for_opener=True)
            session._bind_page(old_page, "paper-retry")
            session._bind_page(old_popup, "paper-retry")
            old_page.handlers["download"](SlowDownload())
            await save_started.wait()
            session._handle_context_page(pending_popup)
            pending_popup.handlers["download"](ImmediateDownload())

            invalidation = asyncio.create_task(
                session.invalidate_paper_downloads("paper-retry")
            )
            await asyncio.sleep(0)
            self.assertFalse(invalidation.done())
            opener_ready.set()
            await asyncio.wait_for(invalidation, timeout=2)
            self.assertIsNone(session._page_papers[id(old_page)])
            self.assertIsNone(session._page_papers[id(old_popup)])
            self.assertEqual(delivered, [])
            self.assertFalse((Path(directory) / "paper-retry" / "manual-old.pdf").exists())

            old_page.handlers["download"](ImmediateDownload())
            await session.wait_for_downloads("paper-retry")
            self.assertEqual(delivered, [])

            new_page = Page()
            session._bind_page(new_page, "paper-retry")
            new_page.handlers["download"](ImmediateDownload())
            await session.wait_for_downloads("paper-retry")
            self.assertEqual(delivered, ["paper-retry"])

    async def test_popup_download_stays_with_its_opener_when_context_event_follows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = BrowserSession(SimpleNamespace(download_dir=Path(directory)))
            saved: list[tuple[str, Path]] = []

            async def callback(paper_id, path):
                saved.append((paper_id, path))

            session.set_download_callback(callback)

            class Page:
                def __init__(self, opener=None):
                    self.handlers = {}
                    self._opener = opener

                def on(self, event, handler):
                    self.handlers[event] = handler

                async def opener(self):
                    return self._opener

            class Download:
                suggested_filename = "article.pdf"

                async def save_as(self, path):
                    Path(path).write_bytes(b"%PDF-1.4\n")

            opener_a = Page()
            opener_b = Page()
            popup_b = Page(opener_b)
            session._bind_page(opener_a, "paper-a")
            session._bind_page(opener_b, "paper-b")
            opener_b.handlers["popup"](popup_b)
            session._handle_context_page(popup_b)
            self.assertEqual(session._page_papers[id(popup_b)], "paper-b")
            popup_b.handlers["download"](Download())
            await session.wait_for_downloads("paper-b")
            self.assertEqual([paper for paper, _ in saved], ["paper-b"])
            self.assertTrue(saved[0][1].exists())

    async def test_wait_for_downloads_includes_popup_waiting_for_opener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = BrowserSession(SimpleNamespace(download_dir=Path(directory)))
            opener_ready = asyncio.Event()
            saved: list[str] = []

            async def callback(paper_id, _path):
                saved.append(paper_id)

            session.set_download_callback(callback)

            class Page:
                def __init__(self, opener=None):
                    self.handlers = {}
                    self._opener = opener

                def on(self, event, handler):
                    self.handlers[event] = handler

                async def opener(self):
                    await opener_ready.wait()
                    return self._opener

            class Download:
                suggested_filename = "article.pdf"

                async def save_as(self, path):
                    Path(path).write_bytes(b"%PDF-1.4\n")

            opener = Page()
            popup = Page(opener)
            session._bind_page(opener, "paper-b")
            session._handle_context_page(popup)
            popup.handlers["download"](Download())
            waiter = asyncio.create_task(session.wait_for_downloads("paper-b"))
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            opener_ready.set()
            await asyncio.wait_for(waiter, timeout=2)
            self.assertEqual(saved, ["paper-b"])

    async def test_stop_papers_cancels_in_flight_pdf_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = InstitutionProfile(id="school", name="School")
            session = BrowserSession(
                SimpleNamespace(institution=profile, download_dir=Path(directory))
            )
            request_started = asyncio.Event()

            class Request:
                async def get(self, *_args, **_kwargs):
                    request_started.set()
                    await asyncio.Event().wait()

            class Page:
                url = "https://publisher.example/article"

                def __init__(self):
                    self.closed = False
                    self.handlers = {}

                def on(self, event, callback):
                    self.handlers[event] = callback

                async def wait_for_timeout(self, _milliseconds):
                    return None

                async def close(self):
                    self.closed = True

            class Context:
                request = Request()

                def __init__(self):
                    self.page = Page()

                async def new_page(self):
                    return self.page

            context = Context()

            async def context_for(_profile):
                return context

            async def open_entry(_page, url, **_kwargs):
                return url

            async def links(_page):
                return [{"text": "PDF", "href": "https://publisher.example/main.pdf"}]

            session._context_for = context_for
            session._open_institution_entry = open_entry
            session._candidate_links = links
            task = asyncio.create_task(
                session.acquire_for_paper(
                    "paper-stop", "https://publisher.example/article", profile=profile
                )
            )
            await asyncio.wait_for(request_started.wait(), timeout=2)
            await session.stop_papers(["paper-stop"])
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(context.page.closed)
            self.assertNotIn("paper-stop", session._paper_contexts)
            self.assertFalse((Path(directory) / "paper-stop" / "institution-main.pdf").exists())

    async def test_same_publisher_tabs_overlap_and_keep_profile_cookies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile_a = InstitutionProfile(id="school-a", name="A")
            profile_b = InstitutionProfile(id="school-b", name="B")
            session = BrowserSession(
                SimpleNamespace(institution=profile_a, download_dir=Path(directory))
            )
            entered = asyncio.Event()
            release = asyncio.Event()
            active = 0
            peak = 0
            requests: list[tuple[str, str]] = []

            class Response:
                status = 200
                headers = {"content-type": "application/pdf"}

                def __init__(self, content: bytes):
                    self.content = content

                async def body(self):
                    return self.content

            class Request:
                def __init__(self, school: str):
                    self.school = school

                async def get(self, url: str, **_kwargs):
                    nonlocal active, peak
                    requests.append((self.school, url))
                    active += 1
                    peak = max(peak, active)
                    if len(requests) == 3:
                        entered.set()
                    await release.wait()
                    active -= 1
                    return Response(f"%PDF-1.4\n{self.school}:{url}".encode())

            class Page:
                def __init__(self):
                    self.url = ""
                    self.closed = False
                    self.handlers = {}

                def on(self, event, handler):
                    self.handlers[event] = handler

                async def wait_for_timeout(self, _milliseconds):
                    return None

                async def title(self):
                    return "Paper"

                async def close(self):
                    self.closed = True

            class Context:
                def __init__(self, school):
                    self.request = Request(school)
                    self.pages = []

                async def new_page(self):
                    page = Page()
                    self.pages.append(page)
                    return page

            contexts = {"school-a": Context("A"), "school-b": Context("B")}

            async def context_for(profile):
                context = contexts[profile.id]
                session._context = context  # Simulate another profile starting.
                await asyncio.sleep(0)
                return context

            async def open_entry(page, url, **_kwargs):
                page.url = url
                return url

            async def links(page):
                return [{"text": "PDF", "href": page.url + ".pdf"}]

            async def unexpected_reveal(_page):
                self.fail("automatic article tabs must not be brought to front")

            session._context_for = context_for
            session._open_institution_entry = open_entry
            session._candidate_links = links
            session._reveal = unexpected_reveal
            urls = [
                ("paper-a1", "https://publisher.example/a1", profile_a),
                ("paper-a2", "https://publisher.example/a2", profile_a),
                ("paper-b1", "https://publisher.example/b1", profile_b),
            ]
            tasks = [
                asyncio.create_task(session.acquire_for_paper(paper, url, profile=profile))
                for paper, url, profile in urls
            ]
            await asyncio.wait_for(entered.wait(), timeout=2)
            self.assertEqual(peak, 3)
            self.assertEqual([school for school, _ in requests].count("A"), 2)
            release.set()
            results = await asyncio.gather(*tasks)
            for (paper, url, profile), result in zip(urls, results):
                content = Path(result["path"]).read_bytes()
                self.assertIn(f"{profile.name}:{url}.pdf".encode(), content)
                self.assertTrue(contexts[profile.id].pages)
                self.assertTrue(contexts[profile.id].pages[-1].closed)

    async def test_ready_manual_session_uses_two_same_publisher_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = InstitutionProfile(id="school", name="School", access_type="manual_browser")
            session = BrowserSession(
                SimpleNamespace(institution=profile, download_dir=Path(directory))
            )
            session.mark_institution_session("publisher.example", "ready", profile=profile)
            both_fetching = asyncio.Event()
            release = asyncio.Event()
            entered: list[str] = []

            class Page:
                def __init__(self):
                    self.url = ""
                    self.handlers = {}
                    self.closed = False

                def on(self, event, handler):
                    self.handlers[event] = handler

                async def goto(self, url, **_kwargs):
                    self.url = url

                async def wait_for_timeout(self, _milliseconds):
                    return None

                def is_closed(self):
                    return self.closed

                async def title(self):
                    return "Paper"

                async def close(self):
                    self.closed = True

            class Context:
                def __init__(self):
                    self.pages = []

                async def new_page(self):
                    page = Page()
                    self.pages.append(page)
                    return page

            context = Context()

            async def context_for(_profile):
                return context

            async def links(page):
                return [{"text": "PDF", "href": page.url + ".pdf"}]

            async def fetch(paper_id, _candidates, destination):
                entered.append(paper_id)
                if len(entered) == 2:
                    both_fetching.set()
                await release.wait()
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-1.4\n")
                return {"source_url": destination.name, "path": str(destination)}

            session._context_for = context_for
            session._candidate_links = links
            session._fetch_pdf = fetch
            tasks = [
                asyncio.create_task(
                    session.acquire_for_paper(
                        paper_id, f"https://publisher.example/{paper_id}", profile=profile
                    )
                )
                for paper_id in ("paper-1", "paper-2")
            ]
            await asyncio.wait_for(both_fetching.wait(), timeout=2)
            self.assertEqual(set(entered), {"paper-1", "paper-2"})
            release.set()
            await asyncio.gather(*tasks)
            self.assertEqual(len(context.pages), 2)
            self.assertTrue(all(page.closed for page in context.pages))

    async def test_doi_preflight_uses_background_tab_and_closes_it(self) -> None:
        profile = InstitutionProfile(id="school-a", name="A")
        session = BrowserSession(SimpleNamespace(institution=profile))

        class Page:
            url = ""
            closed = False

            def on(self, *_args):
                return None

            async def goto(self, _url, **_kwargs):
                self.url = "https://ieeexplore.ieee.org/document/42"

            async def close(self):
                self.closed = True

        page = Page()

        class Context:
            async def new_page(self):
                return page

        async def context_for(_profile):
            return Context()

        session._context_for = context_for
        result = await session.resolve_publisher_for_paper("https://doi.org/10.1000/test")
        self.assertEqual(result, "ieee")
        self.assertTrue(page.closed)

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
