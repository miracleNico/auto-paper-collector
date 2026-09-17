from __future__ import annotations

import asyncio
import hashlib
import html
import re
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse, urlunparse

from .config import Settings
from .credentials import load_credentials
from .downloader import safe_filename
from .institution_access import (
    NeedsManualInstitutionAction,
    SessionState,
    configured_entry_url,
    profile_access_type,
    profile_school_aliases,
    session_key,
)
from .publishers import (
    is_publisher_block,
    publisher_institution_login_candidates,
    publisher_key,
    publisher_pdf_candidates,
)
from .redaction import redact_diagnostic_text, redact_url
from .user_config import InstitutionProfile, extract_doi_from_ezproxy_url, institution_openurl


DownloadCallback = Callable[[str, Path], Awaitable[None]]
DownloadStartCallback = Callable[[], None]


class BrowserError(RuntimeError):
    pass


class LoginTimeoutError(BrowserError):
    pass


class PublisherBlockedError(BrowserError):
    pass


def _focus_window_by_title(title: str) -> None:
    if sys.platform != "win32" or not title.strip():
        return
    needle = title.strip()[:48].casefold()
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        match = ctypes.c_void_p()

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if needle in buffer.value.casefold():
                match.value = hwnd
                return False
            return True

        user32.EnumWindows(callback, 0)
        if match.value:
            user32.ShowWindow(match.value, SW_RESTORE)
            user32.SetForegroundWindow(match.value)
    except Exception:
        return


class BrowserSession:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._context = None
        self._contexts: dict[str, Any] = {}
        self._context_profile_id: str | None = None
        self._lock = asyncio.Lock()
        self._acquire_lock = asyncio.Lock()
        self._active_paper_id: str | None = None
        self._download_callback: DownloadCallback | None = None
        self._blocked_papers: set[str] = set()
        self._bound_pages: set[int] = set()
        self._page_papers: dict[int, str | None] = {}
        self._pages_by_paper: dict[str, dict[int, Any]] = {}
        self._download_tasks: dict[str, set[asyncio.Task]] = {}
        self._institution_pages: dict[str, Any] = {}
        self._institution_targets: dict[str, str] = {}
        self._institution_publishers: dict[str, str] = {}
        self._institution_profiles: dict[str, InstitutionProfile] = {}
        self._institution_session_states: dict[tuple[str, str], SessionState] = {}

    def set_download_callback(self, callback: DownloadCallback) -> None:
        self._download_callback = callback

    def _profile(self, profile: InstitutionProfile | None = None) -> InstitutionProfile:
        return profile or self.settings.institution

    def _profile_id(self, profile: InstitutionProfile) -> str:
        value = str(getattr(profile, "id", "") or "custom").strip().casefold()
        if value == "mcgill":
            return value
        slug = re.sub(r"[^a-z0-9_.-]+", "-", value).strip("-.")[:48]
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        return f"{slug or 'institution'}-{digest}"

    def _profile_dir(self, profile: InstitutionProfile) -> Path:
        base = Path(self.settings.browser_profile_dir)
        profile_id = self._profile_id(profile)
        if profile_id == "mcgill":
            return base
        return base.parent / f"{base.name}-{profile_id}"

    async def start(self, profile: InstitutionProfile | None = None) -> None:
        profile = self._profile(profile)
        profile_id = self._profile_id(profile)
        contexts = getattr(self, "_contexts", {})
        self._contexts = contexts
        if self._context is not None and not contexts and not getattr(
            self, "_context_profile_id", None
        ):
            # Compatibility with tests and callers that inject one context.
            self._contexts[profile_id] = self._context
            self._context_profile_id = profile_id
            return
        if profile_id in contexts:
            self._context = contexts[profile_id]
            self._context_profile_id = profile_id
            return
        async with self._lock:
            if profile_id in self._contexts:
                self._context = self._contexts[profile_id]
                self._context_profile_id = profile_id
                return
            try:
                from playwright.async_api import async_playwright

                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self._profile_dir(profile)),
                    channel="chrome",
                    headless=False,
                    accept_downloads=True,
                    no_viewport=True,
                    args=["--start-maximized", "--new-window"],
                )
                self._contexts[profile_id] = context
                self._context = context
                self._context_profile_id = profile_id
                context.on("page", self._handle_context_page)
                for page in context.pages:
                    self._bind_page(page, None)
                    await self._reveal(page)
            except Exception as exc:
                raise BrowserError(
                    f"无法启动专用 Chrome：{redact_diagnostic_text(exc)}"
                ) from exc

    def _bind_page(self, page, paper_id: str | None) -> None:
        page_key = id(page)
        previous = self._page_papers.get(page_key)
        if previous and previous in self._pages_by_paper:
            self._pages_by_paper[previous].pop(page_key, None)
        self._page_papers[page_key] = paper_id
        if paper_id:
            self._pages_by_paper.setdefault(paper_id, {})[page_key] = page
        if page_key in self._bound_pages:
            return
        self._bound_pages.add(page_key)
        page.on(
            "download",
            lambda download: self._schedule_download(
                download, self._page_papers.get(page_key)
            ),
        )
        page.on("close", lambda *_: self._forget_page(page_key))

    def _handle_context_page(self, page) -> None:
        """Bind popups to their opener, never to a global active-paper hint."""
        self._bind_page(page, None)
        with suppress(RuntimeError):
            asyncio.create_task(self._inherit_opener_binding(page))

    async def _inherit_opener_binding(self, page) -> None:
        try:
            opener = await page.opener()
        except Exception:
            return
        if opener is None:
            return
        self._bind_page(page, self._page_papers.get(id(opener)))

    def _forget_page(self, page_key: int) -> None:
        self._bound_pages.discard(page_key)
        paper_id = self._page_papers.pop(page_key, None)
        if paper_id and paper_id in self._pages_by_paper:
            self._pages_by_paper[paper_id].pop(page_key, None)
            if not self._pages_by_paper[paper_id]:
                self._pages_by_paper.pop(paper_id, None)

    def _schedule_download(self, download, paper_id: str | None) -> None:
        if not paper_id or paper_id in self._blocked_papers:
            return
        task = asyncio.create_task(self._save_download(download, paper_id))
        self._download_tasks.setdefault(paper_id, set()).add(task)
        task.add_done_callback(lambda done: self._forget_download(paper_id, done))

    def _forget_download(self, paper_id: str, task: asyncio.Task) -> None:
        tasks = self._download_tasks.get(paper_id)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._download_tasks.pop(paper_id, None)

    async def _save_download(self, download, paper_id: str | None) -> None:
        if not paper_id or paper_id in self._blocked_papers:
            return
        name = safe_filename(download.suggested_filename or "paper.pdf")
        destination = self.settings.download_dir / paper_id / f"manual-{name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        stem = destination.stem
        suffix = destination.suffix
        counter = 0
        while True:
            candidate = (
                destination
                if counter == 0
                else destination.with_name(f"{stem}-{counter}{suffix}")
            )
            try:
                candidate.touch(exist_ok=False)
                destination = candidate
                break
            except FileExistsError:
                counter += 1
        try:
            await download.save_as(str(destination))
            if paper_id in self._blocked_papers:
                destination.unlink(missing_ok=True)
                return
            if self._download_callback:
                await self._download_callback(paper_id, destination)
        except asyncio.CancelledError:
            destination.unlink(missing_ok=True)
            raise
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    async def open_for_paper(
        self,
        paper_id: str | None,
        url: str,
        *,
        profile: InstitutionProfile | None = None,
    ) -> None:
        if paper_id and paper_id in self._blocked_papers:
            raise BrowserError("论文所属批次正在删除")
        await self.start(profile)
        self._active_paper_id = paper_id
        assert self._context is not None
        page = await self._context.new_page()
        self._bind_page(page, paper_id)
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await self._reveal(page)

    async def _reveal(self, page) -> None:
        with suppress(Exception):
            await page.bring_to_front()
        title = ""
        with suppress(Exception):
            title = await page.title()
            await page.evaluate("window.focus()")
        if title:
            _focus_window_by_title(title)

    def _is_login_url(
        self, url: str, profile: InstitutionProfile | None = None
    ) -> bool:
        profile = self._profile(profile)
        folded = url.casefold()
        markers = profile.login_url_markers or (
            "login.microsoftonline.com",
            "shibboleth",
            "saml",
        )
        if any(marker.casefold() in folded for marker in markers):
            return True
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        path = (parsed.path or "").casefold()
        ezproxy_hosts = {item.casefold() for item in (profile.ezproxy_hosts or ())}
        return bool(host and host in ezproxy_hosts and "/login" in path)

    def _resolver_url(
        self, entry_url: str, profile: InstitutionProfile | None = None
    ) -> str | None:
        profile = self._profile(profile)
        doi = extract_doi_from_ezproxy_url(entry_url)
        if not doi:
            return None
        return institution_openurl(profile, doi)

    def _rewrite_entry_host(self, url: str, host: str) -> str:
        parsed = urlparse(url)
        return urlunparse(parsed._replace(netloc=host))

    def _proxied_publisher_url(self, publisher_url: str, proxy_host: str) -> str:
        publisher = urlparse(publisher_url)
        if not publisher.hostname or publisher.scheme != "https":
            raise BrowserError(
                f"DOI 没有解析到安全的出版社地址：{redact_url(publisher_url)}"
            )
        proxied_host = publisher.hostname.replace(".", "-") + "." + proxy_host
        return urlunparse(
            (
                "https",
                proxied_host,
                publisher.path,
                publisher.params,
                publisher.query,
                publisher.fragment,
            )
        )

    async def _autofill_login(
        self, page, profile: InstitutionProfile | None = None
    ) -> bool:
        profile = self._profile(profile)
        try:
            credentials = load_credentials(profile.id)
        except Exception:
            return False
        if not credentials:
            return False
        password = page.locator("input[type='password']:visible")
        if await password.count() == 0:
            password = page.locator("input[type='password']")
        username = page.locator(
            "input[type='email']:visible, input[name='loginfmt']:visible, "
            "input[name*='user' i]:visible, input[id*='user' i]:visible, "
            "input[type='text']:visible, input[type='email'], input[name='loginfmt']"
        )
        filled = False
        try:
            if await username.count():
                await username.first.fill(credentials["username"], timeout=5_000)
                filled = True
            if await password.count():
                await password.first.fill(credentials["password"], timeout=5_000)
                filled = True
        except Exception:
            return False
        if filled:
            await page.bring_to_front()
        return filled

    async def _wait_until_logged_in(
        self, page, profile: InstitutionProfile | None = None
    ) -> bool:
        deadline = time.monotonic() + max(30, int(self.settings.login_wait_seconds))
        while time.monotonic() < deadline:
            if not self._is_login_url(page.url, profile):
                return True
            await page.wait_for_timeout(1000)
        return False

    async def ensure_logged_in(
        self,
        entry_url: str,
        *,
        profile: InstitutionProfile | None = None,
        publisher: str | None = None,
    ) -> None:
        """Open the institution entry once and wait if a login page appears."""
        profile = self._profile(profile)
        if profile_access_type(profile) != "ezproxy":
            result = await self.open_institution_login(
                entry_url, profile=profile, publisher=publisher
            )
            raise NeedsManualInstitutionAction(
                "login_required",
                publisher=result["publisher"],
                page_url=result["page_url"],
                detail=f"请在 Chrome 中完成 {profile.name} 登录，然后继续检查",
            )
        await self.start(profile)
        name = profile.name
        async with self._acquire_lock:
            await self.start(profile)
            assert self._context is not None
            page = await self._context.new_page()
            self._bind_page(page, None)
            try:
                await self._open_institution_entry(page, entry_url, profile=profile)
                await self._reveal(page)
                await page.wait_for_timeout(2000)
                if not self._is_login_url(page.url, profile):
                    return
                filled = await self._autofill_login(page, profile)
                await self._reveal(page)
                if not await self._wait_until_logged_in(page, profile):
                    hint = f"已填入保存的 {name} 账号；" if filled else ""
                    raise LoginTimeoutError(
                        f"等待登录超时：{hint}请在 Chrome 中完成 {name} 登录或 2FA"
                    )
            finally:
                with suppress(Exception):
                    await page.close()

    def _ensure_institution_runtime(self) -> None:
        if not hasattr(self, "_institution_pages"):
            self._institution_pages = {}
        if not hasattr(self, "_institution_targets"):
            self._institution_targets = {}
        if not hasattr(self, "_institution_publishers"):
            self._institution_publishers = {}
        if not hasattr(self, "_institution_profiles"):
            self._institution_profiles = {}
        if not hasattr(self, "_institution_session_states"):
            self._institution_session_states = {}

    def institution_session_state(
        self, publisher: str, *, profile: InstitutionProfile | None = None
    ) -> str:
        self._ensure_institution_runtime()
        profile = self._profile(profile)
        return self._institution_session_states.get(
            session_key(profile, publisher), SessionState.UNKNOWN
        ).value

    def mark_institution_session(
        self,
        publisher: str,
        state: SessionState | str,
        *,
        profile: InstitutionProfile | None = None,
    ) -> None:
        self._ensure_institution_runtime()
        profile = self._profile(profile)
        self._institution_session_states[session_key(profile, publisher)] = SessionState(state)

    def institution_session_states(self) -> list[dict[str, str]]:
        """Return non-secret, in-memory session observations for the status API."""
        self._ensure_institution_runtime()
        return [
            {
                "institution_id": institution_id,
                "publisher": publisher,
                "state": state.value,
            }
            for (institution_id, publisher), state in sorted(
                self._institution_session_states.items()
            )
        ]

    def detach_institution_page(self, paper_id: str) -> bool:
        """Keep a takeover tab as reusable authentication, not as a paper download tab.

        This is used when the open-access race finishes first after a user has
        already started an institutional login.  The visible page stays open,
        but any later download from it cannot be attached to the completed
        paper.  A later paper for the same institution/publisher can adopt it.
        """
        self._ensure_institution_runtime()
        page = self._institution_pages.pop(paper_id, None)
        target = self._institution_targets.pop(paper_id, None)
        publisher = self._institution_publishers.pop(paper_id, None)
        profile = self._institution_profiles.pop(paper_id, None)
        if page is None:
            return False
        profile = profile or self._profile()
        publisher = publisher or publisher_key(
            str(getattr(page, "url", "") or target or "")
        )
        login_id = (
            f"__institution_login__-{self._profile_id(profile)}-{publisher}"
        )
        existing = self._institution_pages.get(login_id)
        self._bind_page(page, None)
        if existing is None or existing is page:
            self._institution_pages[login_id] = page
            self._institution_targets[login_id] = target or page.url
            self._institution_publishers[login_id] = publisher
            self._institution_profiles[login_id] = profile
        if self._active_paper_id == paper_id:
            self._active_paper_id = None
        return True

    @staticmethod
    async def _action_controls(page) -> list[dict[str, Any]]:
        return await page.locator("a, button, [role='button'], [role='option']").evaluate_all(
            """elements => elements.map((element, index) => ({
                index,
                text: (element.innerText || element.getAttribute('aria-label') || '').trim(),
                href: element.href || element.getAttribute('href') || ''
            }))"""
        )

    async def _open_discovered_institution_login(self, page, publisher: str) -> None:
        if publisher not in {"ieee", "sciencedirect"}:
            raise NeedsManualInstitutionAction(
                "institution_entry_not_found",
                publisher=publisher,
                page_url=page.url,
                detail="该出版社尚无自动机构登录适配，请在 Chrome 中手动导航",
            )
        controls = await self._action_controls(page)
        links = [
            {"text": str(item.get("text") or ""), "href": urljoin(page.url, str(item.get("href") or ""))}
            for item in controls
            if str(item.get("href") or "").strip()
        ]
        candidates = publisher_institution_login_candidates(page.url, links)
        if candidates:
            await page.goto(
                candidates[0]["href"], wait_until="domcontentloaded", timeout=60_000
            )
            return
        matches = [
            item
            for item in controls
            if any(
                token in str(item.get("text") or "").casefold()
                for token in (
                    "institutional sign in",
                    "institution sign in",
                    "access through your institution",
                    "sign in via your institution",
                )
            )
        ]
        if len(matches) == 1:
            await page.locator("a, button, [role='button'], [role='option']").nth(
                int(matches[0]["index"])
            ).click(timeout=15_000)
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            return
        raise NeedsManualInstitutionAction(
            "institution_entry_not_found",
            publisher=publisher,
            page_url=page.url,
        )

    @staticmethod
    def _normalized_school(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]+", " ", value.casefold()).strip()

    async def _select_school_if_present(
        self, page, profile: InstitutionProfile, publisher: str, paper_id: str | None
    ) -> None:
        search = page.locator(
            "input[placeholder*='institution' i], input[aria-label*='institution' i], "
            "input[placeholder*='organization' i], input[aria-label*='organization' i], "
            "input[placeholder*='school' i], input[aria-label*='school' i]"
            + (
                ", input[type='search']"
                if any(
                    token in page.url.casefold()
                    for token in ("wayf", "institution", "federat", "carsi", "shibboleth")
                )
                else ""
            )
        )
        if await search.count() == 0:
            return
        aliases = profile_school_aliases(profile)
        if not aliases:
            raise NeedsManualInstitutionAction(
                "school_not_found", publisher=publisher, paper_id=paper_id, page_url=page.url
            )
        await search.first.fill(aliases[0], timeout=10_000)
        await page.wait_for_timeout(500)
        controls = await self._action_controls(page)
        normalized_aliases = {self._normalized_school(value) for value in aliases}
        matches: list[dict[str, Any]] = []
        for item in controls:
            text = self._normalized_school(str(item.get("text") or ""))
            if not text:
                continue
            if any(alias == text or alias in text or text in alias for alias in normalized_aliases):
                matches.append(item)
        if len(matches) != 1:
            reason = "school_ambiguous" if len(matches) > 1 else "school_not_found"
            raise NeedsManualInstitutionAction(
                reason, publisher=publisher, paper_id=paper_id, page_url=page.url
            )
        selected = matches[0]
        href = str(selected.get("href") or "").strip()
        if href:
            await page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=60_000)
        else:
            await page.locator("a, button, [role='button'], [role='option']").nth(
                int(selected["index"])
            ).click(timeout=15_000)
            with suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)

    async def open_institution_access(
        self,
        paper_id: str,
        target_url: str,
        *,
        publisher: str | None = None,
        profile: InstitutionProfile | None = None,
    ) -> dict[str, Any]:
        """Open and retain one CARSI/manual page for user takeover."""
        if paper_id in self._blocked_papers:
            raise BrowserError("论文所属批次正在删除")
        self._ensure_institution_runtime()
        profile = self._profile(profile)
        access_type = profile_access_type(profile)
        if access_type == "ezproxy":
            raise BrowserError("EZproxy 不使用人工接管入口")
        await self.start(profile)
        async with self._acquire_lock:
            await self.start(profile)
            existing = self._institution_pages.get(paper_id)
            if existing is not None:
                with suppress(Exception):
                    if not existing.is_closed():
                        existing_publisher = self._institution_publishers.get(
                            paper_id, publisher or publisher_key(target_url)
                        )
                        await self._reveal(existing)
                        return {
                            "status": self.institution_session_state(
                                existing_publisher, profile=profile
                            ),
                            "publisher": existing_publisher,
                            "page_url": existing.url,
                            "paper_id": paper_id,
                        }
            assert self._context is not None
            page = await self._context.new_page()
            bound_paper_id = (
                None if paper_id.startswith("__institution_login__-") else paper_id
            )
            self._active_paper_id = bound_paper_id
            self._bind_page(page, bound_paper_id)
            self._institution_pages[paper_id] = page
            self._institution_targets[paper_id] = target_url
            self._institution_profiles[paper_id] = profile
            publisher_hint = publisher if publisher and publisher != "generic" else None
            resolved_publisher = publisher_hint or publisher_key(target_url)
            self._institution_publishers[paper_id] = resolved_publisher
            try:
                # DOI URLs do not identify the publisher. Resolve the article
                # first, then choose a publisher-specific WAYFless URL.
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
                resolved_publisher = publisher_hint or publisher_key(page.url)
                self._institution_publishers[paper_id] = resolved_publisher
                state = self.institution_session_state(resolved_publisher, profile=profile)
                if state in {SessionState.WAITING.value, SessionState.EXPIRED.value}:
                    wanted_key = session_key(profile, resolved_publisher)
                    for other_id, other_page in list(self._institution_pages.items()):
                        if other_id == paper_id:
                            continue
                        other_profile = self._institution_profiles.get(other_id)
                        other_publisher = self._institution_publishers.get(other_id)
                        try:
                            other_closed = bool(other_page.is_closed())
                        except Exception:
                            other_closed = True
                        if other_closed:
                            self._institution_pages.pop(other_id, None)
                            self._institution_targets.pop(other_id, None)
                            self._institution_publishers.pop(other_id, None)
                            self._institution_profiles.pop(other_id, None)
                            self._forget_page(id(other_page))
                            continue
                        if (
                            other_profile is not None
                            and other_publisher is not None
                            and session_key(other_profile, other_publisher) == wanted_key
                        ):
                            with suppress(Exception):
                                await page.close()
                            self._forget_page(id(page))
                            self._institution_pages.pop(paper_id, None)
                            self._institution_targets.pop(paper_id, None)
                            self._institution_publishers.pop(paper_id, None)
                            self._institution_profiles.pop(paper_id, None)
                            self._active_paper_id = self._page_papers.get(id(other_page))
                            await self._reveal(other_page)
                            raise NeedsManualInstitutionAction(
                                "session_expired"
                                if state == SessionState.EXPIRED.value
                                else "login_required",
                                publisher=resolved_publisher,
                                paper_id=paper_id,
                                page_url=other_page.url,
                                detail="已有同出版社登录待处理",
                            )
                if state == SessionState.READY.value:
                    await self._reveal(page)
                    return {
                        "status": SessionState.READY.value,
                        "publisher": resolved_publisher,
                        "page_url": page.url,
                        "paper_id": paper_id,
                    }
                entry_url = configured_entry_url(profile, resolved_publisher, target_url)
                if entry_url and entry_url != page.url:
                    await page.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
                elif access_type == "carsi_saml" and not entry_url:
                    await self._open_discovered_institution_login(page, resolved_publisher)
                if access_type == "carsi_saml":
                    await self._select_school_if_present(
                        page, profile, resolved_publisher, paper_id
                    )
                new_state = (
                    SessionState.EXPIRED
                    if state == SessionState.READY.value
                    and self._is_login_url(page.url, profile)
                    else SessionState.WAITING
                )
                self.mark_institution_session(
                    resolved_publisher, new_state, profile=profile
                )
                await self._reveal(page)
                return {
                    "status": new_state.value,
                    "publisher": resolved_publisher,
                    "page_url": page.url,
                    "paper_id": paper_id,
                }
            except NeedsManualInstitutionAction as exc:
                if paper_id in self._institution_pages:
                    self.mark_institution_session(
                        resolved_publisher, SessionState.WAITING, profile=profile
                    )
                    await self._reveal(page)
                if exc.paper_id is None:
                    raise NeedsManualInstitutionAction(
                        exc.reason,
                        publisher=resolved_publisher,
                        paper_id=paper_id,
                        page_url=page.url,
                        detail=str(exc),
                    ) from exc
                raise

    async def open_institution_login(
        self,
        target_url: str,
        *,
        publisher: str | None = None,
        profile: InstitutionProfile | None = None,
    ) -> dict[str, Any]:
        """Open a reusable authentication tab without binding it to a paper."""
        profile = self._profile(profile)
        login_id = (
            f"__institution_login__-{self._profile_id(profile)}-"
            f"{publisher or publisher_key(target_url)}"
        )
        result = await self.open_institution_access(
            login_id, target_url, publisher=publisher, profile=profile
        )
        result["paper_id"] = None
        return result

    async def _open_ready_institution_target(
        self,
        paper_id: str,
        target_url: str,
        *,
        publisher: str,
        profile: InstitutionProfile,
    ) -> None:
        """Bind a fresh article tab to a previously authenticated session."""
        await self.start(profile)
        async with self._acquire_lock:
            await self.start(profile)
            assert self._context is not None
            page = await self._context.new_page()
            self._bind_page(page, paper_id)
            self._institution_pages[paper_id] = page
            self._institution_targets[paper_id] = target_url
            self._institution_publishers[paper_id] = publisher
            self._institution_profiles[paper_id] = profile
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1000)
            if self._is_login_url(page.url, profile):
                self.mark_institution_session(publisher, SessionState.EXPIRED, profile=profile)
                await self._reveal(page)
                raise NeedsManualInstitutionAction(
                    "session_expired",
                    publisher=publisher,
                    paper_id=paper_id,
                    page_url=page.url,
                )

    @staticmethod
    async def _candidate_links(page) -> list[dict[str, str]]:
        values: list[dict[str, str]] = []
        last_error: Exception | None = None
        for _ in range(4):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                values = await page.locator("a[href]").evaluate_all(
                    """links => links.map(link => ({
                        text: (link.innerText || link.getAttribute('aria-label') || '').trim(),
                        href: link.href || ''
                    })).filter(value => value.href)"""
                )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                message = str(exc).casefold()
                if "execution context was destroyed" in message or "navigation" in message:
                    await page.wait_for_timeout(1500)
                    continue
                raise
        if last_error is not None:
            raise last_error
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for value in values:
            text = value["text"].strip()
            absolute = urljoin(page.url, value["href"].strip())
            parsed = urlparse(absolute)
            haystack = f"{text} {absolute}".casefold()
            if parsed.scheme not in {"http", "https"}:
                continue
            if not any(
                token in haystack
                for token in ("pdf", "pdfft", "download", "stamp", "full-text", "full text")
            ):
                continue
            if absolute not in seen:
                seen.add(absolute)
                result.append({"text": text[:160], "href": absolute})
        return result

    @staticmethod
    def _rank_candidate(value: dict[str, str]) -> tuple[int, int]:
        haystack = f"{value['text']} {value['href']}".casefold()
        score = 0
        if "view pdf" in haystack or "download pdf" in haystack:
            score += 100
        if "ielx" in haystack:
            score += 90
        if "/pdfft" in haystack or ".pdf" in haystack:
            score += 80
        if "stamppdf" in haystack or "getpdf.jsp" in haystack:
            score += 70
        if "stamp" in haystack:
            score += 20
        if "supp" in haystack or "support" in haystack:
            score -= 100
        return score, -len(value["href"])

    @staticmethod
    def _embedded_candidates(markup: str, base_url: str) -> list[str]:
        decoded = html.unescape(markup).replace("\\/", "/")
        discovered: list[str] = []
        for pattern in (
            r"(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]",
            r"https?://[^'\"<>\s]+",
            r"['\"]([^'\"]+\.pdf(?:\?[^'\"]*)?)['\"]",
        ):
            discovered.extend(re.findall(pattern, decoded, flags=re.IGNORECASE))
        result: list[str] = []
        seen: set[str] = set()
        for value in discovered:
            absolute = urljoin(base_url, value)
            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https"}:
                continue
            if not any(token in absolute.casefold() for token in (".pdf", "pdf", "stamp")):
                continue
            if absolute not in seen:
                seen.add(absolute)
                result.append(absolute)
        return result

    async def _fetch_pdf(
        self, paper_id: str, candidates: list[dict[str, str]], destination: Path
    ) -> dict[str, Any]:
        assert self._context is not None
        queue = sorted(candidates, key=self._rank_candidate, reverse=True)
        visited: set[str] = set()
        errors: list[str] = []
        while queue:
            candidate = queue.pop(0)
            url = candidate["href"]
            if url in visited:
                continue
            visited.add(url)
            try:
                response = await self._context.request.get(
                    url,
                    headers={"Accept": "application/pdf,text/html;q=0.8,*/*;q=0.5"},
                    timeout=60_000,
                    fail_on_status_code=False,
                )
                content_type = response.headers.get("content-type", "")
                body = await response.body()
                sample = body[:4000].decode("utf-8", errors="ignore") if body else ""
                if is_publisher_block(response.status, body, sample):
                    raise PublisherBlockedError(
                        f"出版社拒绝：HTTP {response.status} {content_type} {redact_url(url)}"
                    )
                if response.status == 200 and body.startswith(b"%PDF-"):
                    if paper_id in self._blocked_papers:
                        raise BrowserError("论文所属批次正在删除")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(body)
                    return {
                        "source_url": redact_url(url) or url,
                        "link_text": candidate["text"],
                        "status": response.status,
                        "content_type": content_type,
                        "bytes": len(body),
                        "path": str(destination),
                    }
                if response.status == 200 and "html" in content_type.casefold():
                    markup = body.decode("utf-8", errors="ignore")
                    for embedded in self._embedded_candidates(markup, url):
                        if embedded not in visited:
                            queue.append({"text": "Embedded PDF", "href": embedded})
                errors.append(
                    f"HTTP {response.status} {content_type} {redact_url(url)}"
                )
            except PublisherBlockedError:
                raise
            except Exception as exc:
                errors.append(
                    f"{redact_url(url)}: {redact_diagnostic_text(exc)}"
                )
        raise BrowserError("机构页面未返回可验证 PDF：" + " | ".join(errors[:6]))

    async def _open_institution_entry(
        self, page, url: str, *, profile: InstitutionProfile | None = None
    ) -> str:
        profile = self._profile(profile)
        hosts = list(profile.ezproxy_hosts)
        parsed = urlparse(url)
        entry_url = url
        last_error: Exception | None = None
        attempts = [url]
        if parsed.hostname and parsed.hostname in hosts:
            for host in hosts:
                if host != parsed.hostname:
                    attempts.append(self._rewrite_entry_host(url, host))
        for candidate in attempts:
            try:
                await page.goto(candidate, wait_until="domcontentloaded", timeout=60_000)
                return candidate
            except Exception as exc:
                last_error = exc
                if "ERR_CERT_COMMON_NAME_INVALID" not in str(exc):
                    raise
        doi = extract_doi_from_ezproxy_url(url)
        if not doi:
            if last_error:
                raise last_error
            raise BrowserError("无法打开机构入口")
        await page.goto(f"https://doi.org/{doi}", wait_until="domcontentloaded", timeout=60_000)
        proxy_host = hosts[-1] if hosts else (parsed.hostname or "")
        if not proxy_host:
            raise BrowserError(f"DOI 没有可用的代理主机：{redact_url(page.url)}")
        entry_url = self._proxied_publisher_url(page.url, proxy_host)
        await page.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
        return entry_url

    async def continue_institution_access(
        self,
        paper_id: str,
        target_url: str | None = None,
        *,
        publisher: str | None = None,
        profile: InstitutionProfile | None = None,
        on_download_started: DownloadStartCallback | None = None,
    ) -> dict[str, Any]:
        """Resume the retained page after the user completes CARSI/manual access."""
        self._ensure_institution_runtime()
        page = self._institution_pages.get(paper_id)
        profile = profile or self._institution_profiles.get(paper_id) or self._profile()
        adopted = False
        if page is None:
            requested_publisher = publisher or publisher_key(target_url or "")
            reusable: list[tuple[str, Any, str]] = []
            for other_id, other_page in self._institution_pages.items():
                other_profile = self._institution_profiles.get(other_id)
                other_publisher = self._institution_publishers.get(other_id)
                if other_profile is None or other_publisher is None:
                    continue
                if session_key(other_profile, other_publisher) != session_key(
                    profile, other_publisher
                ):
                    continue
                if other_publisher != requested_publisher:
                    continue
                if self.institution_session_state(
                    other_publisher, profile=profile
                ) not in {SessionState.WAITING.value, SessionState.EXPIRED.value}:
                    continue
                try:
                    if other_page.is_closed():
                        continue
                except Exception:
                    continue
                reusable.append((other_id, other_page, other_publisher))
            if len(reusable) == 1:
                other_id, page, adopted_publisher = reusable[0]
                self._institution_pages.pop(other_id, None)
                self._institution_targets.pop(other_id, None)
                self._institution_publishers.pop(other_id, None)
                self._institution_profiles.pop(other_id, None)
                self._bind_page(page, paper_id)
                self._active_paper_id = paper_id
                self._institution_pages[paper_id] = page
                self._institution_targets[paper_id] = target_url or page.url
                self._institution_publishers[paper_id] = adopted_publisher
                self._institution_profiles[paper_id] = profile
                adopted = True
        if page is None:
            raise NeedsManualInstitutionAction(
                "institution_entry_not_found",
                publisher=publisher or publisher_key(target_url or ""),
                paper_id=paper_id,
                page_url=target_url,
                detail="机构访问页面已关闭，请重新打开登录流程",
            )
        target_url = target_url or self._institution_targets.get(paper_id)
        publisher = (
            self._institution_publishers.get(paper_id)
            or publisher
            or publisher_key(target_url or page.url)
        )
        await self.start(profile)
        async with self._acquire_lock:
            await self.start(profile)
            try:
                page_closed = bool(page.is_closed())
            except Exception:
                page_closed = False
            if page_closed:
                self._institution_pages.pop(paper_id, None)
                self._institution_targets.pop(paper_id, None)
                self._institution_publishers.pop(paper_id, None)
                self._institution_profiles.pop(paper_id, None)
                self._forget_page(id(page))
                raise NeedsManualInstitutionAction(
                    "institution_entry_not_found",
                    publisher=publisher,
                    paper_id=paper_id,
                    page_url=target_url,
                    detail="机构访问页面已关闭，请重新打开登录流程",
                )
            previous = self.institution_session_state(publisher, profile=profile)
            if self._is_login_url(page.url, profile):
                state = SessionState.EXPIRED if previous == SessionState.READY.value else SessionState.WAITING
                self.mark_institution_session(publisher, state, profile=profile)
                reason = "session_expired" if state is SessionState.EXPIRED else "login_required"
                await self._reveal(page)
                raise NeedsManualInstitutionAction(
                    reason, publisher=publisher, paper_id=paper_id, page_url=page.url
                )
            if profile_access_type(profile) == "carsi_saml" and target_url:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(1000)
                if publisher == "generic":
                    resolved_publisher = publisher_key(page.url)
                    if resolved_publisher != "generic":
                        publisher = resolved_publisher
                        self._institution_publishers[paper_id] = publisher
                if self._is_login_url(page.url, profile):
                    self.mark_institution_session(publisher, SessionState.EXPIRED, profile=profile)
                    await self._reveal(page)
                    raise NeedsManualInstitutionAction(
                        "session_expired", publisher=publisher, paper_id=paper_id, page_url=page.url
                    )
            if (
                adopted
                and profile_access_type(profile) == "manual_browser"
                and target_url
                and page.url != target_url
            ):
                self.mark_institution_session(
                    publisher, SessionState.WAITING, profile=profile
                )
                await self._reveal(page)
                raise NeedsManualInstitutionAction(
                    "manual_navigation_required",
                    publisher=publisher,
                    paper_id=paper_id,
                    page_url=page.url,
                    detail="请在当前机构页面导航到这篇论文，再继续检查",
                )
            self.mark_institution_session(publisher, SessionState.READY, profile=profile)
            await self._reveal(page)
            page_candidates = await self._candidate_links(page)
            candidates: list[dict[str, str]] = []
            seen_hrefs: set[str] = set()
            for item in publisher_pdf_candidates(page.url) + page_candidates:
                href = item.get("href") or ""
                if href and href not in seen_hrefs:
                    seen_hrefs.add(href)
                    candidates.append(item)
            if not candidates:
                raise NeedsManualInstitutionAction(
                    "pdf_entry_not_found",
                    publisher=publisher,
                    paper_id=paper_id,
                    page_url=page.url,
                )
            destination = self.settings.download_dir / paper_id / "institution-main.pdf"
            if on_download_started:
                on_download_started()
            result = await self._fetch_pdf(paper_id, candidates, destination)
            result.update(
                {
                    "entry_url": configured_entry_url(profile, publisher, target_url or "") or target_url,
                    "publisher": publisher,
                    "publisher_url": page.url,
                    "publisher_title": await page.title(),
                }
            )
            if paper_id in self._blocked_papers:
                destination.unlink(missing_ok=True)
                raise BrowserError("论文所属批次正在删除")
            if self._download_callback:
                await self._download_callback(paper_id, destination)
            self._institution_pages.pop(paper_id, None)
            self._institution_targets.pop(paper_id, None)
            self._institution_publishers.pop(paper_id, None)
            self._institution_profiles.pop(paper_id, None)
            with suppress(Exception):
                await page.close()
            return result

    async def acquire_for_paper(
        self,
        paper_id: str,
        url: str,
        *,
        on_download_started: DownloadStartCallback | None = None,
        publisher: str | None = None,
        profile: InstitutionProfile | None = None,
    ) -> dict[str, Any]:
        """Retrieve one PDF through an already user-authenticated Chrome session."""
        if paper_id in self._blocked_papers:
            raise BrowserError("论文所属批次正在删除")
        profile = self._profile(profile)
        if profile_access_type(profile) != "ezproxy":
            self._ensure_institution_runtime()
            if paper_id in self._institution_pages:
                retained = self._institution_pages[paper_id]
                try:
                    retained_closed = bool(retained.is_closed())
                except Exception:
                    retained_closed = True
                if retained_closed:
                    self._institution_pages.pop(paper_id, None)
                    self._institution_targets.pop(paper_id, None)
                    self._institution_publishers.pop(paper_id, None)
                    self._institution_profiles.pop(paper_id, None)
                    self._forget_page(id(retained))
                else:
                    return await self.continue_institution_access(
                        paper_id,
                        url,
                        publisher=publisher,
                        profile=profile,
                        on_download_started=on_download_started,
                    )
            publisher = publisher or publisher_key(url)
            if self.institution_session_state(publisher, profile=profile) == SessionState.READY.value:
                await self._open_ready_institution_target(
                    paper_id,
                    url,
                    publisher=publisher,
                    profile=profile,
                )
                return await self.continue_institution_access(
                    paper_id,
                    url,
                    publisher=publisher,
                    profile=profile,
                    on_download_started=on_download_started,
                )
            opened = await self.open_institution_access(
                paper_id,
                url,
                publisher=publisher if publisher != "generic" else None,
                profile=profile,
            )
            if opened["status"] == SessionState.READY.value:
                return await self.continue_institution_access(
                    paper_id,
                    url,
                    publisher=publisher,
                    profile=profile,
                    on_download_started=on_download_started,
                )
            raise NeedsManualInstitutionAction(
                "session_expired" if opened["status"] == SessionState.EXPIRED.value else "login_required",
                publisher=opened["publisher"],
                paper_id=paper_id,
                page_url=opened["page_url"],
                detail=f"请在 Chrome 中完成 {profile.name} 登录，然后继续检查",
            )
        await self.start(profile)
        async with self._acquire_lock:
            await self.start(profile)
            self._active_paper_id = paper_id
            assert self._context is not None
            page = await self._context.new_page()
            self._bind_page(page, paper_id)
            name = profile.name
            try:
                entry_url = await self._open_institution_entry(page, url, profile=profile)
                await self._reveal(page)
                await page.wait_for_timeout(5000)
                if self._is_login_url(page.url, profile):
                    filled = await self._autofill_login(page, profile)
                    await self._reveal(page)
                    if not await self._wait_until_logged_in(page, profile):
                        hint = f"已填入保存的 {name} 账号；" if filled else ""
                        raise LoginTimeoutError(
                            f"等待登录超时：{hint}请在 Chrome 中完成 {name} 登录或 2FA"
                        )
                page_candidates = await self._candidate_links(page)
                publisher_candidates = publisher_pdf_candidates(page.url)
                candidates: list[dict[str, str]] = []
                seen_hrefs: set[str] = set()
                for item in publisher_candidates + page_candidates:
                    href = item.get("href") or ""
                    if href and href not in seen_hrefs:
                        seen_hrefs.add(href)
                        candidates.append(item)
                if not candidates:
                    raise BrowserError(
                        f"出版社页面没有可识别的 PDF 入口：{redact_url(page.url)}"
                    )
                destination = self.settings.download_dir / paper_id / "institution-main.pdf"
                if on_download_started:
                    on_download_started()
                try:
                    result = await self._fetch_pdf(paper_id, candidates, destination)
                except PublisherBlockedError:
                    raise
                except BrowserError as publisher_error:
                    resolver_url = self._resolver_url(url, profile)
                    if not resolver_url:
                        raise publisher_error
                    await page.goto(resolver_url, wait_until="domcontentloaded", timeout=60_000)
                    try:
                        await page.locator("a[href*='libkey.io']").first.wait_for(
                            state="attached", timeout=20_000
                        )
                    except Exception:
                        await page.wait_for_timeout(2000)
                    resolver_candidates = await self._candidate_links(page)
                    resolver_candidates = publisher_pdf_candidates(page.url) + resolver_candidates
                    if not resolver_candidates:
                        raise BrowserError(
                            f"{name} 馆藏解析器没有提供 PDF 入口：{redact_url(page.url)}; "
                            f"出版社尝试为 {redact_diagnostic_text(publisher_error)}"
                        )
                    result = await self._fetch_pdf(paper_id, resolver_candidates, destination)
                result.update({"entry_url": entry_url, "publisher_url": page.url, "publisher_title": await page.title()})
                if paper_id in self._blocked_papers:
                    destination.unlink(missing_ok=True)
                    raise BrowserError("论文所属批次正在删除")
                if self._download_callback:
                    await self._download_callback(paper_id, destination)
                return result
            finally:
                with suppress(Exception):
                    await page.close()

    async def stop_papers(self, paper_ids: list[str]) -> None:
        paper_set = set(paper_ids)
        self._blocked_papers.update(paper_set)
        if self._active_paper_id in paper_set:
            self._active_paper_id = None
        pages = [
            page
            for paper_id in paper_set
            for page in self._pages_by_paper.get(paper_id, {}).values()
        ]
        retained_pages = [
            self._institution_pages.get(paper_id)
            for paper_id in paper_set
            if getattr(self, "_institution_pages", {}).get(paper_id) is not None
        ]
        pages.extend(page for page in retained_pages if page not in pages)
        if pages:
            await asyncio.gather(
                *(page.close() for page in pages), return_exceptions=True
            )
        downloads = [
            task
            for paper_id in paper_set
            for task in self._download_tasks.get(paper_id, set())
            if not task.done()
        ]
        if downloads:
            await asyncio.gather(*downloads, return_exceptions=True)
        for paper_id in paper_set:
            getattr(self, "_institution_pages", {}).pop(paper_id, None)
            getattr(self, "_institution_targets", {}).pop(paper_id, None)
            getattr(self, "_institution_publishers", {}).pop(paper_id, None)
            getattr(self, "_institution_profiles", {}).pop(paper_id, None)

    async def wait_for_downloads(self, paper_id: str) -> None:
        """Drain downloads that were already started for one paper."""
        while True:
            downloads = [
                task
                for task in self._download_tasks.get(paper_id, set())
                if not task.done()
            ]
            if not downloads:
                return
            await asyncio.gather(*downloads, return_exceptions=True)

    async def close(self) -> None:
        downloads = [
            task for tasks in self._download_tasks.values() for task in tasks
            if not task.done()
        ]
        contexts = list({id(value): value for value in getattr(self, "_contexts", {}).values()}.values())
        if not contexts and self._context is not None:
            contexts = [self._context]
        if contexts:
            await asyncio.gather(
                *(context.close() for context in contexts), return_exceptions=True
            )
        self._context = None
        getattr(self, "_contexts", {}).clear()
        self._context_profile_id = None
        if downloads:
            await asyncio.gather(*downloads, return_exceptions=True)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None
        self._page_papers.clear()
        self._pages_by_paper.clear()
        self._bound_pages.clear()
        getattr(self, "_institution_pages", {}).clear()
        getattr(self, "_institution_targets", {}).clear()
        getattr(self, "_institution_publishers", {}).clear()
        getattr(self, "_institution_profiles", {}).clear()
        # Authentication state is deliberately memory-only and is not trusted
        # after a browser restart.
        getattr(self, "_institution_session_states", {}).clear()
