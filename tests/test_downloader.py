from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from paper_endnote.downloader import download_pdf


class _Response:
    headers = {"content-type": "application/pdf"}

    def __init__(self, first_chunk_written: asyncio.Event) -> None:
        self.first_chunk_written = first_chunk_written

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, _size: int):
        yield b"%PDF-partial"
        self.first_chunk_written.set()
        await asyncio.sleep(60)


class _Stream:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, *_args, **_kwargs):
        return _Stream(self.response)


class DownloaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "paper.pdf"
            started = asyncio.Event()
            client = _Client(_Response(started))
            settings = SimpleNamespace(
                request_timeout_seconds=30,
                max_pdf_bytes=100 * 1024 * 1024,
            )
            with patch("paper_endnote.downloader.httpx.AsyncClient", return_value=client):
                task = asyncio.create_task(
                    download_pdf("https://example.invalid/paper.pdf", destination, settings)
                )
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".pdf.part").exists())


if __name__ == "__main__":
    unittest.main()
