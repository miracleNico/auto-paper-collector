from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from paper_endnote.clients import CrossrefClient, _retry_after_seconds
from paper_endnote.downloader import close_download_clients, download_pdf
from paper_endnote.pdf_worker import close_pdf_workers, validate_pdf_async


class _CrossrefHTTP:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.calls = 0
        self.responses: list[int | tuple[int, dict[str, str]]] = []
        self.call_times: list[float] = []

    async def get(self, url, **_kwargs):
        self.calls += 1
        self.call_times.append(asyncio.get_running_loop().time())
        if self.responses:
            queued = self.responses.pop(0)
            status, headers = queued if isinstance(queued, tuple) else (queued, {})
            return httpx.Response(
                status, request=httpx.Request("GET", url), headers=headers, json={"message": {}}
            )
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await self.release.wait()
            return httpx.Response(200, request=httpx.Request("GET", url), json={"message": {}})
        finally:
            self.active -= 1

    async def aclose(self):
        return None


class _PDFResponse:
    status_code = 200
    headers = {"content-type": "application/pdf"}

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, _size):
        yield b"%PDF-test"


class _RedirectResponse:
    status_code = 302
    headers = {"location": "https://b.invalid/paper.pdf"}


class _DownloadHTTP:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.by_host: dict[str, int] = {}
        self.host_peaks: dict[str, int] = {}

    @asynccontextmanager
    async def stream(self, _method, url, **_kwargs):
        if httpx.URL(url).path == "/redirect":
            yield _RedirectResponse()
            return
        host = httpx.URL(url).host
        self.active += 1
        self.by_host[host] = self.by_host.get(host, 0) + 1
        self.peak = max(self.peak, self.active)
        self.host_peaks[host] = max(self.host_peaks.get(host, 0), self.by_host[host])
        try:
            await self.release.wait()
            yield _PDFResponse()
        finally:
            self.active -= 1
            self.by_host[host] -= 1

    async def aclose(self):
        return None


class NetworkParallelTests(unittest.IsolatedAsyncioTestCase):
    async def test_crossref_honors_long_retry_after(self) -> None:
        request = httpx.Request("GET", "https://api.crossref.org/works/10.1000%2Fa")
        numeric = httpx.Response(429, request=request, headers={"Retry-After": "240"})
        self.assertEqual(_retry_after_seconds(numeric, 0), 240.0)

        when = datetime.now(timezone.utc) + timedelta(minutes=5)
        dated = httpx.Response(
            429, request=request, headers={"Retry-After": format_datetime(when, usegmt=True)}
        )
        self.assertGreater(_retry_after_seconds(dated, 0), 240.0)

    async def test_crossref_public_and_polite_concurrency(self) -> None:
        for mailto, expected_peak in (("", 1), ("research@example.org", 3)):
            fake = _CrossrefHTTP()
            settings = SimpleNamespace(
                request_timeout_seconds=30,
                crossref_mailto=mailto,
                crossref_min_interval_seconds=0,
            )
            with patch("paper_endnote.clients.httpx.AsyncClient", return_value=fake):
                client = CrossrefClient(settings)
            tasks = [
                asyncio.create_task(client._get(f"https://api.crossref.org/works/10.1000%2F{i}"))
                for i in range(4)
            ]
            await asyncio.sleep(0.05)
            self.assertEqual(fake.peak, expected_peak)
            fake.release.set()
            await asyncio.gather(*tasks)

    async def test_crossref_retries_429(self) -> None:
        fake = _CrossrefHTTP()
        fake.responses = [(429, {"retry-after": "0.05"}), 200]
        settings = SimpleNamespace(
            request_timeout_seconds=30,
            crossref_mailto="",
            crossref_min_interval_seconds=0,
        )
        with patch("paper_endnote.clients.httpx.AsyncClient", return_value=fake):
            client = CrossrefClient(settings)
        self.assertEqual(
            await client._get("https://api.crossref.org/works/10.1000%2Fa"),
            {"message": {}},
        )
        self.assertEqual(fake.calls, 2)
        self.assertGreaterEqual(fake.call_times[1] - fake.call_times[0], 0.045)

    async def test_oa_shared_total_and_host_caps(self) -> None:
        fake = _DownloadHTTP()
        settings = SimpleNamespace(request_timeout_seconds=30, max_pdf_bytes=1000)
        with tempfile.TemporaryDirectory() as directory:
            with patch("paper_endnote.downloader.httpx.AsyncClient", return_value=fake) as constructor:
                tasks = [
                    asyncio.create_task(
                        download_pdf(f"https://{host}.invalid/{index}.pdf", Path(directory) / f"{host}{index}.pdf", settings)
                    )
                    for host, index in (("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2), ("b", 3))
                ]
                await asyncio.sleep(0.05)
                self.assertLessEqual(fake.peak, 4)
                self.assertEqual(fake.host_peaks.get("a.invalid"), 2)
                fake.release.set()
                await asyncio.gather(*tasks)
                self.assertEqual(constructor.call_count, 1)
                await close_download_clients()

    async def test_oa_redirect_uses_final_host_quota(self) -> None:
        fake = _DownloadHTTP()
        settings = SimpleNamespace(request_timeout_seconds=30, max_pdf_bytes=1000)
        with tempfile.TemporaryDirectory() as directory:
            with patch("paper_endnote.downloader.httpx.AsyncClient", return_value=fake):
                tasks = [
                    asyncio.create_task(
                        download_pdf("https://a.invalid/redirect", Path(directory) / f"{index}.pdf", settings)
                    )
                    for index in range(3)
                ]
                await asyncio.sleep(0.05)
                self.assertEqual(fake.host_peaks.get("b.invalid"), 2)
                fake.release.set()
                await asyncio.gather(*tasks)
                await close_download_clients()

    async def test_pdf_validation_uses_process_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.pdf"
            path.write_bytes(b"not a PDF")
            result = await validate_pdf_async(
                path, expected_doi="10.1000/a", expected_title="A", ocr_enabled=False
            )
            self.assertFalse(result.valid_pdf)
        await close_pdf_workers()


if __name__ == "__main__":
    unittest.main()
