from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .config import Settings


class DownloadError(RuntimeError):
    pass


def safe_filename(value: str, fallback: str = "paper.pdf") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not name:
        name = fallback
    if not name.casefold().endswith(".pdf"):
        name += ".pdf"
    return name[:180]


def filename_from_url(url: str, fallback: str) -> str:
    name = unquote(Path(urlparse(url).path).name)
    return safe_filename(name if ".pdf" in name.casefold() else fallback)


async def download_pdf(url: str, destination: Path, settings: Settings) -> dict[str, str | int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    headers = {
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
        "User-Agent": "PaperEndNote/0.1 (personal research workflow)",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, follow_redirects=True, headers=headers) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                size = 0
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > settings.max_pdf_bytes:
                            raise DownloadError("PDF 超过 100 MB 限制")
                        handle.write(chunk)
        with temporary.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise DownloadError(f"下载内容不是 PDF（Content-Type: {content_type or 'unknown'}）")
        os.replace(temporary, destination)
        return {"path": str(destination), "bytes": size, "content_type": content_type}
    except asyncio.CancelledError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
