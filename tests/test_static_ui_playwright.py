from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "paper_endnote" / "static"


class _StaticHandler(BaseHTTPRequestHandler):
    """Serve only the checked-in UI assets; API calls are mocked in Playwright."""

    _assets = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    }

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        asset = self._assets.get(urlsplit(self.path).path)
        if asset is None:
            self.send_error(404)
            return
        filename, content_type = asset
        content = (STATIC / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class StaticUiPlaywrightTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _StaticHandler)
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever,
            name="static-ui-test-server",
            daemon=True,
        )
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    async def asyncSetUp(self) -> None:
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(
                channel="chrome", headless=True
            )
        except PlaywrightError as error:
            await self.playwright.stop()
            self.skipTest(f"本机没有可用于 UI 测试的 Chrome：{error}")

        self.page = await self.browser.new_page(locale="zh-CN")
        await self.page.route("**/api/**", self._mock_api)

    async def asyncTearDown(self) -> None:
        browser = getattr(self, "browser", None)
        if browser is not None:
            await browser.close()
        playwright = getattr(self, "playwright", None)
        if playwright is not None:
            await playwright.stop()

    async def _mock_api(self, route) -> None:
        path = urlsplit(route.request.url).path
        if path == "/api/state":
            payload = {
                "settings": {
                    "crossref_email": "",
                    "unpaywall_email": "",
                    "endnote_library": "",
                    "acquisition_sources": ["open_access", "institution"],
                    "auto_institution": True,
                    "auto_commit": False,
                    "login_wait_seconds": 600,
                    "ocr_enabled": False,
                    "ocr_languages": "eng",
                    "ocr_max_pages": 2,
                    "institution": {
                        "preset": "",
                        "id": "example-university",
                        "name": "示例大学",
                        "access_type": "ezproxy",
                        "login_url": "",
                        "ezproxy_login": "https://proxy.example/login?url={url}",
                        "ezproxy_hosts": ["proxy.example"],
                        "openurl": "",
                        "login_url_markers": [],
                        "school_aliases": [],
                        "entity_id": "",
                        "publisher_login_urls": {},
                    },
                },
                "presets": [],
                "credentials": {"username": "", "password_saved": False},
                "zotero": {"ready": False, "enabled": False, "running": False},
                "ocr": {"ready": False},
                "endnote": {"library": "", "library_exists": False},
            }
        elif path == "/api/batches":
            payload = [
                {
                    "id": "test-batch",
                    "name": "Offline UI Batch",
                    "status": "paused",
                    "completed": 0,
                    "total": 1,
                    "deletion_status": "",
                }
            ]
        elif path == "/api/batches/test-batch":
            payload = {
                "id": "test-batch",
                "name": "Offline UI Batch",
                "status": "paused",
                "target_library": "Offline Test",
                "endnote_export_path": "",
                "error": "",
                "papers": [
                    {
                        "id": "paper-1",
                        "position": 1,
                        "title": "A Synthetic Paper Used Only for UI Testing",
                        "doi": "10.0000/offline-ui-test",
                        "year": 2026,
                        "metadata_status": "verified",
                        "pdf_status": "not_found",
                        "endnote_status": "pending",
                        "status": "needs_pdf",
                        "needs_action": "manual_pdf",
                        "version": "",
                        "error": "",
                    }
                ],
            }
        else:
            await route.fulfill(
                status=404,
                content_type="application/json; charset=utf-8",
                body=json.dumps({"detail": f"unmocked API: {path}"}),
            )
            return

        await route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(payload, ensure_ascii=False),
        )

    async def test_paper_menu_survives_polling_and_has_close_control(self) -> None:
        await self.page.goto(f"{self.base_url}/index.html")
        await self.page.get_by_role("button", name="任务", exact=True).click()
        await self.page.locator('[data-batch-open="test-batch"]').click()

        menu = self.page.locator(".paper-more-actions")
        summary = menu.locator("summary")
        await expect(summary).to_be_visible()
        await summary.click()
        await expect(menu).to_have_attribute("open", "")

        # The UI polls every 2.6 seconds.  Keep the menu open through a complete
        # polling interval and verify that the live refresh does not dismiss it.
        await self.page.wait_for_timeout(3100)
        self.assertGreaterEqual(await self.page.evaluate("state.pollCount"), 1)
        await expect(menu).to_have_attribute("open", "")

        await menu.get_by_role("button", name="关闭更多操作菜单").click()
        await expect(menu).not_to_have_attribute("open", "")
        await expect(summary).to_be_focused()

    async def test_institution_access_type_switches_visible_fields(self) -> None:
        await self.page.goto(f"{self.base_url}/index.html")
        await self.page.get_by_role("button", name="设置", exact=True).click()

        access_type = self.page.locator("#institution-access-type")
        ezproxy_fields = self.page.locator('[data-institution-types="ezproxy"]')
        carsi_fields = self.page.locator('[data-institution-types="carsi_saml"]')

        await access_type.select_option("carsi_saml")
        self.assertTrue(await ezproxy_fields.evaluate("node => node.hidden"))
        self.assertFalse(await carsi_fields.evaluate("node => node.hidden"))
        await expect(self.page.locator("#carsi-experimental")).to_be_visible()
        await expect(self.page.locator("#credentials-form")).to_be_hidden()

        await access_type.select_option("manual_browser")
        self.assertTrue(await ezproxy_fields.evaluate("node => node.hidden"))
        self.assertTrue(await carsi_fields.evaluate("node => node.hidden"))
        await expect(self.page.locator("#carsi-experimental")).to_be_hidden()
        await expect(self.page.locator("#credentials-form")).to_be_hidden()


if __name__ == "__main__":
    unittest.main()
