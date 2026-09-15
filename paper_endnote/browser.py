from __future__ import annotations

import asyncio
import html
import re
from contextlib import suppress
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from .clients import mcgill_worldcat_url
from .config import Settings
from .downloader import safe_filename


DownloadCallback = Callable[[str, Path], Awaitable[None]]


class BrowserError(RuntimeError):
    pass


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
                )
                self._context.on("page", lambda page: self._bind_page(page, self._active_paper_id))
                for page in self._context.pages:
                    self._bind_page(page, None)
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
        await page.bring_to_front()

    @staticmethod
    def _is_login_url(url: str) -> bool:
        folded = url.casefold()
        return any(
            marker in folded
            for marker in ("login.microsoftonline.com", "shibboleth", "saml", "idp.mcgill.ca")
        )

    @staticmethod
    async def _candidate_links(page) -> list[dict[str, str]]:
        values: list[dict[str, str]] = await page.locator("a[href]").evaluate_all(
            """links => links.map(link => ({
                text: (link.innerText || link.getAttribute('aria-label') || '').trim(),
                href: link.href || ''
            })).filter(value => value.href)"""
        )
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
        parsed_page = urlparse(page.url)
        science_direct = re.search(
            r"/science/article/pii/([^/?#]+)", parsed_page.path, re.IGNORECASE
        )
        if science_direct:
            pii = science_direct.group(1)
            pdfft = (
                f"{parsed_page.scheme}://{parsed_page.netloc}/science/article/pii/{pii}/pdfft"
                "?isDTMRedir=true&download=true"
            )
            if pdfft not in seen:
                result.insert(0, {"text": "ScienceDirect PDF", "href": pdfft})
        return result

    @staticmethod
    def _worldcat_resolver_url(entry_url: str) -> str | None:
        target = parse_qs(urlparse(entry_url).query).get("url", [""])[0]
        parsed = urlparse(target)
        if parsed.hostname not in {"doi.org", "dx.doi.org"}:
            return None
        doi = parsed.path.lstrip("/").strip()
        if not doi:
            return None
        return mcgill_worldcat_url(doi)

    @staticmethod
    def _rank_candidate(value: dict[str, str]) -> tuple[int, int]:
        haystack = f"{value['text']} {value['href']}".casefold()
        score = 0
        if "view pdf" in haystack or "download pdf" in haystack:
            score += 100
        if "/pdfft" in haystack or ".pdf" in haystack:
            score += 80
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
            except Exception as exc:
                errors.append(f"{url}: {exc}")
        raise BrowserError("机构页面未返回可验证 PDF：" + " | ".join(errors[:6]))

    async def acquire_for_paper(self, paper_id: str, url: str) -> dict[str, Any]:
        """Retrieve one PDF through an already user-authenticated Chrome session."""
        await self.start()
        async with self._acquire_lock:
            self._active_paper_id = paper_id
            assert self._context is not None
            page = await self._context.new_page()
            self._bind_page(page, paper_id)
            try:
                entry_url = url
                try:
                    await page.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
                except Exception as exc:
                    # McGill's generic EZproxy entry currently redirects to
                    # the proxy3 cluster.  If that redirect presents a stale
                    # certificate, retry the same official EZproxy endpoint on
                    # the already-authenticated cluster without ignoring TLS.
                    if (
                        "ERR_CERT_COMMON_NAME_INVALID" not in str(exc)
                        or "https://proxy.library.mcgill.ca/" not in entry_url
                    ):
                        raise
                    entry_url = entry_url.replace(
                        "https://proxy.library.mcgill.ca/",
                        "https://proxy3.library.mcgill.ca/",
                        1,
                    )
                    try:
                        await page.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
                    except Exception as retry_exc:
                        if "ERR_CERT_COMMON_NAME_INVALID" not in str(retry_exc):
                            raise
                        original = parse_qs(urlparse(url).query).get("url", [""])[0]
                        if not original.startswith(("https://doi.org/", "http://doi.org/")):
                            raise
                        await page.goto(original, wait_until="domcontentloaded", timeout=60_000)
                        publisher = urlparse(page.url)
                        if not publisher.hostname or publisher.scheme != "https":
                            raise BrowserError(f"DOI 没有解析到安全的出版社地址：{page.url}")
                        proxied_host = publisher.hostname.replace(".", "-") + ".proxy3.library.mcgill.ca"
                        entry_url = urlunparse(
                            (
                                "https",
                                proxied_host,
                                publisher.path,
                                publisher.params,
                                publisher.query,
                                publisher.fragment,
                            )
                        )
                        await page.goto(entry_url, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(5000)
                if self._is_login_url(page.url):
                    raise BrowserError("McGill 登录或 2FA 尚未完成；请先点击‘打开获取页’完成登录")
                candidates = await self._candidate_links(page)
                if not candidates:
                    raise BrowserError(f"出版社页面没有可识别的 PDF 入口：{page.url}")
                destination = self.settings.download_dir / paper_id / "institution-main.pdf"
                try:
                    result = await self._fetch_pdf(candidates, destination)
                except BrowserError as publisher_error:
                    resolver_url = self._worldcat_resolver_url(url)
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
                    if not resolver_candidates:
                        raise BrowserError(
                            f"McGill 馆藏解析器没有提供 PDF 入口：{page.url}; "
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
