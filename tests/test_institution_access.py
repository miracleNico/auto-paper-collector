from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from paper_endnote.browser import BrowserSession
from paper_endnote.institution_access import (
    ManualInstitutionAction,
    NeedsManualInstitutionAction,
    SessionState,
    configured_entry_url,
    profile_access_type,
)
from paper_endnote.publishers import (
    publisher_institution_login_candidates,
    publisher_key,
)
from paper_endnote.user_config import InstitutionProfile


def _profile(**changes) -> InstitutionProfile:
    values = {
        "id": "example-u",
        "name": "Example University",
        "access_type": "carsi_saml",
        "login_url": "",
        "login_url_markers": ("idp.example.edu",),
        "openurl": "",
        "ezproxy_login": "",
        "ezproxy_hosts": (),
        "school_aliases": ("Example University", "示例大学"),
        "entity_id": "https://idp.example.edu/idp/shibboleth",
        "publisher_login_urls": {"ieee": "https://wayf.example.test/ieee"},
        "preset": "",
    }
    values.update(changes)
    return InstitutionProfile(**values)


class InstitutionAccessPureTests(unittest.TestCase):
    def test_manual_action_redacts_page_url_and_detail(self) -> None:
        action = ManualInstitutionAction(
            reason="login_required",
            publisher="ieee",
            paper_id="paper-1",
            page_url=(
                "https://idp.example/sso?entityID=stable&"
                "SAMLRequest=request-secret&RelayState=relay-secret"
            ),
            detail="See https://idp.example/callback?code=oauth-secret",
        ).as_dict()

        self.assertIn("entityID=stable", action["page_url"])
        self.assertNotIn("request-secret", action["page_url"])
        self.assertNotIn("relay-secret", action["page_url"])
        self.assertNotIn("oauth-secret", action["detail"])
        self.assertIn("[REDACTED]", action["page_url"])

    def test_publisher_keys_cover_first_party_and_proxy_hosts(self) -> None:
        self.assertEqual(publisher_key("https://ieeexplore.ieee.org/document/1"), "ieee")
        self.assertEqual(
            publisher_key("https://www-sciencedirect-com.proxy.example/science/article/pii/1"),
            "www-sciencedirect-com.proxy.example",
        )
        self.assertEqual(
            publisher_key("https://journals.example/article/1"), "journals.example"
        )
        self.assertNotEqual(
            publisher_key("https://onlinelibrary.wiley.com/article/1"),
            publisher_key("https://link.springer.com/article/2"),
        )
        self.assertNotEqual(
            publisher_key("https://ieee-login.evil.example/collect"), "ieee"
        )
        self.assertNotEqual(
            publisher_key("https://elsevier.attacker.example/collect"),
            "sciencedirect",
        )

    def test_configured_carsi_url_has_publisher_precedence(self) -> None:
        profile = _profile(login_url="https://library.example.edu/carsi")
        self.assertEqual(
            configured_entry_url(profile, "ieee", "https://ieeexplore.ieee.org/document/1"),
            "https://wayf.example.test/ieee",
        )
        self.assertEqual(
            configured_entry_url(profile, "generic", "https://journals.example/article/1"),
            "https://library.example.edu/carsi",
        )
        manual = _profile(
            access_type="manual_browser",
            login_url="https://library.example.edu/manual",
            publisher_login_urls={"ieee": "https://stale.example/ieee"},
        )
        self.assertEqual(
            configured_entry_url(
                manual, "ieee", "https://ieeexplore.ieee.org/document/1"
            ),
            "https://library.example.edu/manual",
        )

    def test_carsi_rejects_template_and_legacy_profile_stays_ezproxy(self) -> None:
        self.assertIsNone(
            configured_entry_url(
                _profile(login_url="https://library.example/login?url={target_url}"),
                "generic",
                "https://journals.example/article/1",
            )
        )
        legacy = SimpleNamespace(access_type="", ezproxy_login="https://proxy/login?url={url}")
        self.assertEqual(profile_access_type(legacy), "ezproxy")

    def test_ieee_institution_link_is_preferred_to_personal_login(self) -> None:
        links = [
            {"text": "Personal sign in", "href": "https://idp.ieee.org/personal"},
            {
                "text": "Institutional Sign In",
                "href": "https://idp.ieee.org/institutional-sign-in",
            },
        ]
        candidates = publisher_institution_login_candidates(
            "https://ieeexplore.ieee.org/document/1", links
        )
        self.assertEqual(candidates[0]["href"], links[1]["href"])

    def test_sciencedirect_accepts_only_first_party_institution_links(self) -> None:
        links = [
            {
                "text": "Access through your institution",
                "href": "https://www.sciencedirect.com/user/institution/login",
            },
            {
                "text": "Institutional Sign In",
                "href": "https://login.attacker.example/collect",
            },
        ]
        candidates = publisher_institution_login_candidates(
            "https://www.sciencedirect.com/science/article/pii/S123", links
        )
        self.assertEqual(candidates, [links[0]])

    def test_unknown_and_external_login_links_are_not_followed(self) -> None:
        self.assertEqual(
            publisher_institution_login_candidates(
                "https://journals.example/article/1",
                [
                    {
                        "text": "Institutional Sign In",
                        "href": "https://login.attacker.example/collect",
                    }
                ],
            ),
            [],
        )
        self.assertEqual(
            publisher_institution_login_candidates(
                "https://ieeexplore.ieee.org/document/1",
                [
                    {
                        "text": "Institutional Sign In",
                        "href": "https://login.attacker.example/collect",
                    }
                ],
            ),
            [],
        )
        self.assertEqual(
            publisher_institution_login_candidates(
                "https://ieeexplore.ieee.org/document/1",
                [
                    {
                        "text": "Institutional Sign In",
                        "href": "https://ieee-login.evil.example/collect",
                    }
                ],
            ),
            [],
        )
        self.assertEqual(
            publisher_institution_login_candidates(
                "https://www.sciencedirect.com/science/article/pii/S123",
                [
                    {
                        "text": "Access through your institution",
                        "href": "https://elsevier.attacker.example/collect",
                    }
                ],
            ),
            [],
        )


class _FakeLocator:
    def __init__(self, page: "_FakePage", selector: str, index: int | None = None):
        self.page = page
        self.selector = selector
        self.index = index

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return 1 if self.selector.startswith("input[") and self.page.school_search else 0

    async def fill(self, value: str, timeout: int | None = None) -> None:
        self.page.filled = value

    async def evaluate_all(self, _script: str):
        if self.selector == "a[href]":
            return list(self.page.pdf_links)
        return [dict(item, index=index) for index, item in enumerate(self.page.controls)]

    def nth(self, index: int):
        return _FakeLocator(self.page, self.selector, index)

    async def click(self, timeout: int | None = None) -> None:
        selected = self.page.controls[int(self.index or 0)]
        if selected.get("href"):
            await self.page.goto(selected["href"])


class _FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.school_search = False
        self.filled = ""
        self.controls: list[dict[str, str]] = []
        self.pdf_links: list[dict[str, str]] = []
        self.closed = False
        self.history: list[str] = []

    def on(self, *_args, **_kwargs) -> None:
        return None

    async def goto(self, url: str, **_kwargs):
        self.history.append(url)
        if url.startswith("https://doi.org/"):
            url = "https://ieeexplore.ieee.org/document/1"
        self.url = url
        self.school_search = url in {
            "https://wayf.example.test/ieee",
            "https://wayf.example.test/sciencedirect",
        }
        if self.school_search:
            self.controls = [
                {"text": "Example University", "href": "https://idp.example.edu/login"}
            ]
            self.pdf_links = []
        elif "ieeexplore.ieee.org/document" in url:
            self.controls = []
            self.pdf_links = [
                {
                    "text": "Download PDF",
                    "href": "https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?arnumber=1",
                }
            ]
        elif "sciencedirect.com/science/article/pii" in url:
            self.controls = []
            self.pdf_links = [
                {
                    "text": "View PDF",
                    "href": f"{url.rstrip('/')}/pdfft?download=true",
                }
            ]
        return None

    def locator(self, selector: str):
        return _FakeLocator(self, selector)

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    async def bring_to_front(self) -> None:
        return None

    async def title(self) -> str:
        return "Example article"

    async def evaluate(self, _script: str) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class _FakeContext:
    def __init__(self):
        self.pages: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page


class InstitutionAccessBrowserTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, directory: str, profile: InstitutionProfile):
        settings = SimpleNamespace(
            institution=profile,
            browser_profile_dir=Path(directory) / "profile",
            download_dir=Path(directory) / "downloads",
            login_wait_seconds=60,
        )
        session = BrowserSession(settings)
        session._context = _FakeContext()
        return session

    async def test_standalone_ezproxy_login_page_is_not_bound_to_a_paper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile(
                access_type="ezproxy",
                ezproxy_login="https://proxy.example/login?url={url}",
                ezproxy_hosts=("proxy.example",),
            )
            session = self.make_session(directory, profile)
            await session.open_for_paper(
                None, "https://proxy.example/login?url=https://doi.org/", profile=profile
            )
            page = session._context.pages[-1]
            self.assertIsNone(session._page_papers[id(page)])

    async def test_carsi_unique_school_and_continue_same_paper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            opened = await session.open_institution_access(
                "paper-1", "https://ieeexplore.ieee.org/document/1", profile=profile
            )
            self.assertEqual(opened["status"], "waiting")
            self.assertEqual(opened["publisher"], "ieee")
            page = session._institution_pages["paper-1"]
            self.assertEqual(session._page_papers[id(page)], "paper-1")
            self.assertEqual(page.filled, "Example University")
            self.assertEqual(page.url, "https://idp.example.edu/login")

            # Simulate the user completing the IdP flow in the retained tab.
            page.url = "https://ieeexplore.ieee.org/document/1"

            async def fetch(_paper_id, _candidates, destination: Path):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-1.4\n")
                return {"source_url": "https://ieeexplore.ieee.org/main.pdf", "path": str(destination)}

            session._fetch_pdf = fetch
            result = await session.continue_institution_access(
                "paper-1", profile=profile
            )
            self.assertIn("main.pdf", result["source_url"])
            self.assertEqual(
                session.institution_session_state("ieee", profile=profile),
                SessionState.READY.value,
            )
            self.assertNotIn("paper-1", session._institution_pages)

    async def test_sciencedirect_carsi_school_selection_and_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            article = "https://www.sciencedirect.com/science/article/pii/S123"
            profile = _profile(
                publisher_login_urls={
                    "sciencedirect": "https://wayf.example.test/sciencedirect"
                }
            )
            session = self.make_session(directory, profile)
            opened = await session.open_institution_access(
                "paper-sd", article, profile=profile
            )
            self.assertEqual(opened["publisher"], "sciencedirect")
            page = session._institution_pages["paper-sd"]
            self.assertEqual(page.filled, "Example University")
            self.assertEqual(page.url, "https://idp.example.edu/login")

            page.url = article

            async def fetch(_paper_id, _candidates, destination: Path):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-1.4\n")
                return {
                    "source_url": f"{article}/pdfft?download=true",
                    "path": str(destination),
                }

            session._fetch_pdf = fetch
            result = await session.continue_institution_access(
                "paper-sd", article, profile=profile
            )
            self.assertEqual(result["publisher"], "sciencedirect")
            self.assertEqual(
                session.institution_session_state("sciencedirect", profile=profile),
                SessionState.READY.value,
            )

    async def test_ambiguous_school_keeps_page_for_manual_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            context = session._context
            original_new_page = context.new_page

            async def ambiguous_page():
                page = await original_new_page()
                original_goto = page.goto

                async def goto(url: str, **kwargs):
                    await original_goto(url, **kwargs)
                    if page.school_search:
                        page.controls.append(
                            {
                                "text": "Example University Medical School",
                                "href": "https://idp.example.edu/medical",
                            }
                        )

                page.goto = goto
                return page

            context.new_page = ambiguous_page
            with self.assertRaises(NeedsManualInstitutionAction) as raised:
                await session.open_institution_access(
                    "paper-2", "https://ieeexplore.ieee.org/document/2", profile=profile
                )
            self.assertEqual(raised.exception.reason, "school_ambiguous")
            self.assertIn("paper-2", session._institution_pages)

    async def test_waiting_publisher_reuses_existing_login_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            await session.open_institution_access(
                "paper-1", "https://ieeexplore.ieee.org/document/1", profile=profile
            )
            with self.assertRaises(NeedsManualInstitutionAction) as raised:
                await session.open_institution_access(
                    "paper-2", "https://ieeexplore.ieee.org/document/2", profile=profile
                )
            self.assertEqual(str(raised.exception), "已有同出版社登录待处理")
            self.assertEqual(sum(not page.closed for page in session._context.pages), 1)
            self.assertNotIn("paper-2", session._institution_pages)

    async def test_waiting_login_page_can_be_adopted_by_another_paper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            await session.open_institution_access(
                "paper-1", "https://ieeexplore.ieee.org/document/1", profile=profile
            )
            with self.assertRaises(NeedsManualInstitutionAction):
                await session.open_institution_access(
                    "paper-2", "https://ieeexplore.ieee.org/document/2", profile=profile
                )
            page = session._institution_pages["paper-1"]
            page.url = "https://ieeexplore.ieee.org/document/1"

            async def fetch(_paper_id, _candidates, destination: Path):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-1.4\n")
                return {"source_url": "https://ieeexplore.ieee.org/adopted.pdf", "path": str(destination)}

            session._fetch_pdf = fetch
            result = await session.continue_institution_access(
                "paper-2",
                "https://ieeexplore.ieee.org/document/2",
                profile=profile,
            )
            self.assertIn("adopted.pdf", result["source_url"])
            self.assertNotIn("paper-1", session._institution_pages)
            self.assertEqual(session._page_papers[id(page)], "paper-2")

    async def test_doi_redirect_selects_ieee_carsi_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            opened = await session.open_institution_access(
                "paper-doi", "https://doi.org/10.1109/example", profile=profile
            )
            page = session._institution_pages["paper-doi"]
            self.assertEqual(opened["publisher"], "ieee")
            self.assertEqual(
                page.history[:2],
                ["https://doi.org/10.1109/example", "https://wayf.example.test/ieee"],
            )

    async def test_standalone_login_page_is_not_bound_to_a_paper_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            result = await session.open_institution_login(
                "https://doi.org/10.1109/example", profile=profile
            )
            login_pages = list(session._institution_pages.values())
            self.assertEqual(result["paper_id"], None)
            self.assertEqual(len(login_pages), 1)
            self.assertIsNone(session._page_papers[id(login_pages[0])])

    async def test_closed_takeover_page_is_removed_before_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            await session.open_institution_access(
                "paper-closed",
                "https://ieeexplore.ieee.org/document/1",
                profile=profile,
            )
            old_page = session._institution_pages["paper-closed"]
            await old_page.close()

            with self.assertRaises(NeedsManualInstitutionAction) as raised:
                await session.continue_institution_access(
                    "paper-closed",
                    "https://ieeexplore.ieee.org/document/1",
                    publisher="ieee",
                    profile=profile,
                )
            self.assertEqual(raised.exception.reason, "institution_entry_not_found")
            self.assertNotIn("paper-closed", session._institution_pages)

            reopened = await session.open_institution_access(
                "paper-closed",
                "https://ieeexplore.ieee.org/document/1",
                profile=profile,
            )
            self.assertEqual(reopened["status"], "waiting")
            self.assertIsNot(session._institution_pages["paper-closed"], old_page)

    async def test_generic_request_cannot_adopt_another_publisher_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            await session.open_institution_access(
                "paper-ieee",
                "https://ieeexplore.ieee.org/document/1",
                profile=profile,
            )
            session.detach_institution_page("paper-ieee")

            with self.assertRaises(NeedsManualInstitutionAction) as raised:
                await session.continue_institution_access(
                    "paper-generic",
                    "https://doi.org/10.1000/unknown",
                    publisher="generic",
                    profile=profile,
                )
            self.assertEqual(raised.exception.reason, "institution_entry_not_found")

    async def test_oa_winner_detaches_takeover_page_for_later_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            await session.open_institution_access(
                "paper-oa", "https://ieeexplore.ieee.org/document/1", profile=profile
            )
            page = session._institution_pages["paper-oa"]

            self.assertTrue(session.detach_institution_page("paper-oa"))
            self.assertNotIn("paper-oa", session._institution_pages)
            self.assertIsNone(session._page_papers[id(page)])
            self.assertTrue(
                any(
                    key.startswith("__institution_login__-") and value is page
                    for key, value in session._institution_pages.items()
                )
            )

            # The next paper can adopt the same authentication page after the
            # user completes login, without leaving downloads bound to paper-oa.
            page.url = "https://ieeexplore.ieee.org/document/1"

            async def fetch(_paper_id, _candidates, destination: Path):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-1.4\n")
                return {
                    "source_url": "https://ieeexplore.ieee.org/reused.pdf",
                    "path": str(destination),
                }

            session._fetch_pdf = fetch
            result = await session.continue_institution_access(
                "paper-next",
                "https://ieeexplore.ieee.org/document/2",
                publisher="ieee",
                profile=profile,
            )
            self.assertIn("reused.pdf", result["source_url"])
            self.assertEqual(session._page_papers[id(page)], "paper-next")

    async def test_ready_publisher_skips_login_entry_for_next_paper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            session.mark_institution_session("ieee", SessionState.READY, profile=profile)

            async def fetch(_paper_id, _candidates, destination: Path):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-1.4\n")
                return {"source_url": "https://ieeexplore.ieee.org/next.pdf", "path": str(destination)}

            session._fetch_pdf = fetch
            result = await session.acquire_for_paper(
                "paper-next", "https://ieeexplore.ieee.org/document/2", profile=profile
            )
            self.assertIn("next.pdf", result["source_url"])
            self.assertTrue(
                all(page.url != "https://wayf.example.test/ieee" for page in session._context.pages)
            )

    async def test_generic_takeover_is_reclassified_after_doi_redirect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            page = await session._context.new_page()
            page.url = "https://library.example.test/authenticated"
            session._institution_pages["paper-generic"] = page
            session._institution_targets["paper-generic"] = "https://doi.org/10.1000/a"
            session._institution_publishers["paper-generic"] = "generic"
            session._institution_profiles["paper-generic"] = profile

            async def fetch(_paper_id, _candidates, destination: Path):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-1.4\n")
                return {
                    "source_url": "https://ieeexplore.ieee.org/reclassified.pdf",
                    "path": str(destination),
                }

            session._fetch_pdf = fetch
            result = await session.continue_institution_access(
                "paper-generic",
                "https://doi.org/10.1000/a",
                publisher="generic",
                profile=profile,
            )

            self.assertEqual(result["publisher"], "ieee")
            self.assertEqual(
                session.institution_session_state("ieee", profile=profile),
                SessionState.READY.value,
            )

    async def test_waiting_page_does_not_become_ready_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            session.mark_institution_session("ieee", SessionState.READY, profile=profile)
            session._institution_session_states.clear()
            self.assertEqual(
                session.institution_session_state("ieee", profile=profile),
                SessionState.UNKNOWN.value,
            )

    async def test_login_without_full_text_does_not_report_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile(
                access_type="manual_browser",
                login_url="",
                publisher_login_urls={},
            )
            session = self.make_session(directory, profile)
            page = await session._context.new_page()
            page.url = "https://journal.example.test/article/1"
            page.pdf_links = []
            session._institution_pages["paper-no-access"] = page
            session._institution_targets["paper-no-access"] = page.url
            session._institution_publishers["paper-no-access"] = "generic"
            session._institution_profiles["paper-no-access"] = profile

            with self.assertRaises(NeedsManualInstitutionAction) as raised:
                await session.continue_institution_access(
                    "paper-no-access", profile=profile
                )
            self.assertEqual(raised.exception.reason, "pdf_entry_not_found")
            self.assertIn("paper-no-access", session._institution_pages)

    async def test_ready_session_returning_to_idp_is_marked_expired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = _profile()
            session = self.make_session(directory, profile)
            page = await session._context.new_page()
            page.url = "https://idp.example.edu/login"
            session._institution_pages["paper-expired"] = page
            session._institution_targets["paper-expired"] = (
                "https://ieeexplore.ieee.org/document/1"
            )
            session._institution_publishers["paper-expired"] = "ieee"
            session._institution_profiles["paper-expired"] = profile
            session.mark_institution_session("ieee", SessionState.READY, profile=profile)

            with self.assertRaises(NeedsManualInstitutionAction) as raised:
                await session.continue_institution_access(
                    "paper-expired", profile=profile
                )
            self.assertEqual(raised.exception.reason, "session_expired")
            self.assertEqual(
                session.institution_session_state("ieee", profile=profile),
                SessionState.EXPIRED.value,
            )


if __name__ == "__main__":
    unittest.main()
