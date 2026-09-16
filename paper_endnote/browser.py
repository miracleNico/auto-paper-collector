from __future__ import annotations

import asyncio
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
from .publishers import is_publisher_block, publisher_pdf_candidates
from .user_config import extract_doi_from_ezproxy_url, institution_openurl


DownloadCallback = Callable[[str, Path], Awaitable[None]]


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
        self._lock = asyncio.Lock()
        self._acquire_lock = asyncio.Lock()
        self._active_paper_id: str | None = None
        self._download_callback: DownloadCallback | None = None

    def set_download_callback(self, callback: DownloadCallback) -> None:
        self._download_callback = callback

    async def start(self) -> None:
        if self._context is not None:
            return
        async with self._lock:
            if self._context is not None:
                return
            try:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.settings.browser_profile_dir),
                    channel="chrome",
                    headless=False,
                    accept_downloads=True,
                    no_viewport=True,
                    args=["--start-maximized", "--new-window"],
                )
                self._context.on("page", lambda page: self._bind_page(page, self._active_paper_id))
                for page in self._context.pages:
                    self._bind_page(page, None)
                    await self._reveal(page)
            except Exception as exc:
                await self.close()
                raise BrowserError(f"无法启动专用 Chrome：{exc}") from exc

    def _bind_page(self, page, paper_id: str | None) -> None:
        page.on("download", lambda download: asyncio.create_task(self._save_download(download, paper_id)))

    async def _save_download(self, download, paper_id: str | None) -> None:
        if not paper_id:
            return
        name = safe_filename(download.suggested_filename or "paper.pdf")
        destination = self.settings.download_dir / paper_id / f"manual-{name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        await download.save_as(str(destination))
        if self._download_callback:
            await self._download_callback(paper_id, destination)

    async def open_for_paper(self, paper_id: str, url: str) -> None:
        await self.start()
        self._active_paper_id = paper_id
        assert self._context is not None
        page = await self._context.new_page()
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

    def _is_login_url(self, url: str) -> bool:
        folded = url.casefold()
        markers = self.settings.institution.login_url_markers or (
            "login.microsoftonline.com",
            "shibboleth",
            "saml",
        )
        if any(marker.casefold() in folded for marker in markers):
            return True
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        path = (parsed.path or "").casefold()
        ezproxy_hosts = {item.casefold() for item in (self.settings.institution.ezproxy_hosts or ())}
        return bool(host and host in ezproxy_hosts and "/login" in path)

    def _resolver_url(self, entry_url: str) -> str | None:
        doi = extract_doi_from_ezproxy_url(entry_url)
        if not doi:
            return None
        return institution_openurl(self.settings.institution, doi)

    def _rewrite_entry_host(self, url: str, host: str) -> str:
        parsed = urlparse(url)
        return urlunparse(parsed._replace(netloc=host))

    def _proxied_publisher_url(self, publisher_url: str, proxy_host: str) -> str:
        publisher = urlparse(publisher_url)
        if not publisher.hostname or publisher.scheme != "https":
            raise BrowserError(f"DOI 没有解析到安全的出版社地址：{publisher_url}")
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

    async def _autofill_login(self, page) -> bool:
        try:
            credentials = load_credentials(self.settings.institution.id)
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

    async def _wait_until_logged_in(self, page) -> bool:
        deadline = time.monotonic() + max(30, int(self.settings.login_wait_seconds))
        while time.monotonic() < deadline:
            if not self._is_login_url(page.url):
                return True
            await page.wait_for_timeout(1000)
        return False

    async def ensure_logged_in(self, entry_url: str) -> None:
        """Open the institution entry once and wait if a login page appears."""
        await self.start()
        name = self.settings.institution.name
        async with self._acquire_lock:
            assert self._context is not None
            page = await self._context.new_page()
            try:
                await self._open_institution_entry(page, entry_url)
                await self._reveal(page)
                await page.wait_for_timeout(2000)
                if not self._is_login_url(page.url):
                    return
                filled = await self._autofill_login(page)
                await self._reveal(page)
                if not await self._wait_until_logged_in(page):
                    hint = f"已填入保存的 {name} 账号；" if filled else ""
                    raise LoginTimeoutError(
                        f"等待登录超时：{hint}请在 Chrome 中完成 {name} 登录或 2FA"
                    )
            finally:
                with suppress(Exception):
                    await page.close()

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

    async def _fetch_pdf(self, candidates: list[dict[str, str]], destination: Path) -> dict[str, Any]:
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
                        f"出版社拒绝：HTTP {response.status} {content_type} {url}"
                    )
                if response.status == 200 and body.startswith(b"%PDF-"):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(body)
                    return {
                        "source_url": url,
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
                errors.append(f"HTTP {response.status} {content_type} {url}")
            except PublisherBlockedError:
                raise
            except Exception as exc:
                errors.append(f"{url}: {exc}")
        raise BrowserError("机构页面未返回可验证 PDF：" + " | ".join(errors[:6]))

    async def _open_institution_entry(self, page, url: str) -> str:
        hosts = list(self.settings.institution.ezproxy_hosts)
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
            raise BrowserError(f"DOI 没有可用的代理主机：{page.url}")
        entry_url = self._proxied_publisher_url(page.url, proxy_host)
        await page.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
        return entry_url

    async def acquire_for_paper(self, paper_id: str, url: str) -> dict[str, Any]:
        """Retrieve one PDF through an already user-authenticated Chrome session."""
        await self.start()
        async with self._acquire_lock:
            self._active_paper_id = paper_id
            assert self._context is not None
            page = await self._context.new_page()
            self._bind_page(page, paper_id)
            name = self.settings.institution.name
            try:
                entry_url = await self._open_institution_entry(page, url)
                await self._reveal(page)
                await page.wait_for_timeout(5000)
                if self._is_login_url(page.url):
                    filled = await self._autofill_login(page)
                    await self._reveal(page)
                    if not await self._wait_until_logged_in(page):
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
                    raise BrowserError(f"出版社页面没有可识别的 PDF 入口：{page.url}")
                destination = self.settings.download_dir / paper_id / "institution-main.pdf"
                try:
                    result = await self._fetch_pdf(candidates, destination)
                except PublisherBlockedError:
                    raise
                except BrowserError as publisher_error:
                    resolver_url = self._resolver_url(url)
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
                            f"{name} 馆藏解析器没有提供 PDF 入口：{page.url}; "
                            f"出版社尝试为 {publisher_error}"
                        )
                    result = await self._fetch_pdf(resolver_candidates, destination)
                result.update({"entry_url": entry_url, "publisher_url": page.url, "publisher_title": await page.title()})
                if self._download_callback:
                    await self._download_callback(paper_id, destination)
                return result
            finally:
                with suppress(Exception):
                    await page.close()

    async def close(self) -> None:
        if self._context is not None:
            try:
                with suppress(Exception):
                    await self._context.close()
            finally:
                self._context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None
