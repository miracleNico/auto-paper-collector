from __future__ import annotations

import asyncio
import os
import re
import tempfile
import weakref
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .config import Settings


class DownloadError(RuntimeError):
    pass


class _DownloadPool:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
        self.total = asyncio.Semaphore(4)
        self.hosts: dict[str, asyncio.Semaphore] = {}

    def host_limit(self, url: str) -> asyncio.Semaphore:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DownloadError("PDF 下载地址必须是 HTTP 或 HTTPS URL")
        host = parsed.hostname.casefold()
        return self.hosts.setdefault(host, asyncio.Semaphore(2))


_download_pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _DownloadPool] = weakref.WeakKeyDictionary()


def _download_pool() -> _DownloadPool:
    loop = asyncio.get_running_loop()
    pool = _download_pools.get(loop)
    if pool is None:
        pool = _DownloadPool()
        _download_pools[loop] = pool
    return pool


async def close_download_clients() -> None:
    """Close the calling event loop's reusable OA HTTP connection pool."""
    pool = _download_pools.pop(asyncio.get_running_loop(), None)
    if pool is not None:
        close = getattr(pool.client, "aclose", None)
        if close is not None:
            await close()


def safe_filename(value: str, fallback: str = "paper.pdf") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not name:
        name = fallback
    if not name.casefold().endswith(".pdf"):
        name += ".pdf"
    if len(name) > 180:
        name = name[:-4][:176].rstrip(" .") + ".pdf"
    return name


def filename_from_url(url: str, fallback: str) -> str:
    name = unquote(Path(urlparse(url).path).name)
    return safe_filename(name if ".pdf" in name.casefold() else fallback)


async def download_pdf(url: str, destination: Path, settings: Settings) -> dict[str, str | int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    headers = {
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
        "User-Agent": "PaperEndNote/0.1 (personal research workflow)",
    }
    pool = _download_pool()
    try:
        current_url = url
        for _ in range(11):
            # Acquire the host quota first, so same-host waiters cannot occupy
            # all global slots while downloads from other hosts are ready.
            async with pool.host_limit(current_url), pool.total:
                async with pool.client.stream(
                    "GET", current_url, headers=headers, timeout=settings.request_timeout_seconds
                ) as response:
                    if getattr(response, "status_code", 200) in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise DownloadError("PDF 下载重定向缺少 Location")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").casefold()
                    size = 0
                    fd, filename = tempfile.mkstemp(
                        prefix=destination.name + ".", suffix=".part", dir=destination.parent
                    )
                    temporary = Path(filename)
                    with os.fdopen(fd, "wb") as handle:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            size += len(chunk)
                            if size > settings.max_pdf_bytes:
                                raise DownloadError("PDF 超过 100 MB 限制")
                            handle.write(chunk)
                    with temporary.open("rb") as handle:
                        if handle.read(5) != b"%PDF-":
                            raise DownloadError(
                                f"下载内容不是 PDF（Content-Type: {content_type or 'unknown'}）"
                            )
                    os.replace(temporary, destination)
                    temporary = None
                    return {"path": str(destination), "bytes": size, "content_type": content_type}
        raise DownloadError("PDF 下载重定向次数超过限制")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
