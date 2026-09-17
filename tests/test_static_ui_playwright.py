from __future__ import annotations

import asyncio
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
        self.api_requests: list[tuple[str, dict]] = []
        self.sources_delay = 0.0
        self.sources_plan: list[dict] = []
        self.collections_delay = 0.0
        self.collections_fail = False
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
        try:
            request_payload = json.loads(route.request.post_data or "{}")
        except json.JSONDecodeError:
            request_payload = {}
        self.api_requests.append((path, request_payload))
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
        elif path == "/api/tools/sources":
            plan = self.sources_plan.pop(0) if self.sources_plan else {}
            delay = float(plan.get("delay", self.sources_delay))
            if delay:
                await asyncio.sleep(delay)
            personal_key = plan.get("key", "PERSONAL1")
            personal_name = plan.get("name", "Personal Papers")
            payload = {
                "batches": [],
                "collections": [
                    {"key": personal_key, "name": personal_name, "numItems": 2}
                ],
                "zotero_libraries": [
                    {"id": "user:0", "name": "My Library", "type": "user"},
                    {"id": "group:42", "name": "Lab Library", "type": "group"},
                ],
                "zotero_error": None,
                "zotero_library_error": None,
                "endnote_library": r"C:\Libraries\Default.enl",
                "downloads_dir": r"C:\Users\tester\Downloads",
            }
        elif path == "/api/tools/zotero-collections":
            if self.collections_delay:
                await asyncio.sleep(self.collections_delay)
            if self.collections_fail:
                await route.fulfill(
                    status=409,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps({"detail": "collection lookup failed"}),
                )
                return
            payload = {
                "library_id": "group:42",
                "collections": [
                    {
                        "key": "GROUP001",
                        "name": "Group Papers",
                        "path": "Lab / Group Papers",
                        "numItems": 3,
                    }
                ],
            }
        elif path == "/api/tools/pick-endnote-library":
            payload = {"path": r"C:\Libraries\Selected.enl", "cancelled": False}
        elif path == "/api/tools/export-pdfs/candidates":
            payload = {
                "items": [
                    {
                        "id": "ITEM001",
                        "title": "Synthetic PDF",
                        "available": True,
                        "file_count": 1,
                        "size": 128,
                    }
                ],
                "available_count": 1,
                "available_size": 128,
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

    async def test_pdf_tools_select_zotero_scope_and_endnote_library(self) -> None:
        await self.page.goto(f"{self.base_url}/index.html")
        await self.page.get_by_role("button", name="工具", exact=True).click()

        library = self.page.locator("#export-zotero-library")
        await expect(library.locator("option")).to_have_count(2)
        await library.select_option("group:42")
        await expect(self.page.locator("#export-collection")).to_have_value("GROUP001")
        await expect(self.page.locator("#export-destination")).to_have_value(
            r"C:\Users\tester\Downloads\Lab _ Group Papers"
        )

        await self.page.wait_for_timeout(50)
        candidate_payloads = [
            payload
            for path, payload in self.api_requests
            if path == "/api/tools/export-pdfs/candidates"
        ]
        self.assertEqual(candidate_payloads[-1]["zotero_library_id"], "group:42")
        self.assertEqual(candidate_payloads[-1]["zotero_library_name"], "Lab Library")
        self.assertEqual(candidate_payloads[-1]["collection_key"], "GROUP001")
        self.assertFalse(candidate_payloads[-1]["whole_library"])

        await self.page.locator("#export-source").select_option("endnote")
        await self.page.locator("#pick-export-endnote-library").click()
        await expect(self.page.locator("#export-endnote-library")).to_have_value(
            r"C:\Libraries\Selected.enl"
        )
        await self.page.wait_for_timeout(50)
        candidate_payloads = [
            payload
            for path, payload in self.api_requests
            if path == "/api/tools/export-pdfs/candidates"
        ]
        self.assertEqual(
            candidate_payloads[-1]["endnote_library"],
            r"C:\Libraries\Selected.enl",
        )

    async def test_pdf_tools_stay_disabled_until_initial_sources_are_loaded(self) -> None:
        self.sources_delay = 0.2
        await self.page.goto(f"{self.base_url}/index.html")
        rename_submit = self.page.locator("#rename-pdfs-form button[type='submit']")
        await expect(rename_submit).to_be_disabled()

        await self.page.get_by_role("button", name="工具", exact=True).click()
        await self.page.wait_for_timeout(40)
        await expect(rename_submit).to_be_disabled()
        await expect(self.page.locator("#rename-collection")).to_be_disabled()

        await expect(self.page.locator("#rename-collection")).to_have_value(
            "PERSONAL1", timeout=2000
        )
        await expect(rename_submit).to_be_enabled()

    async def test_zotero_scope_loading_disables_actions_and_recovers_after_failure(self) -> None:
        await self.page.goto(f"{self.base_url}/index.html")
        await self.page.get_by_role("button", name="工具", exact=True).click()
        export_submit = self.page.locator("#export-pdfs-form button[type='submit']")
        await expect(export_submit).to_be_enabled()

        self.collections_delay = 0.2
        await self.page.locator("#export-zotero-library").select_option("group:42")
        await self.page.wait_for_timeout(40)
        await expect(self.page.locator("#export-collection")).to_be_disabled()
        await expect(export_submit).to_be_disabled()
        await expect(self.page.locator("#export-collection")).to_have_value(
            "GROUP001", timeout=2000
        )
        await expect(export_submit).to_be_enabled()

        self.collections_delay = 0.0
        self.collections_fail = True
        await self.page.locator("#rename-zotero-library").select_option("group:42")
        await expect(self.page.locator("#rename-collection")).to_be_disabled()
        await expect(
            self.page.locator("#rename-pdfs-form button[type='submit']")
        ).to_be_disabled()

        self.collections_fail = False
        await self.page.get_by_role("button", name="工具", exact=True).click()
        await expect(self.page.locator("#rename-collection")).to_have_value("PERSONAL1")
        await expect(self.page.locator("#rename-collection")).to_be_enabled()
        await expect(
            self.page.locator("#rename-pdfs-form button[type='submit']")
        ).to_be_enabled()

    async def test_repeated_tools_load_ignores_the_older_sources_response(self) -> None:
        self.sources_plan = [
            {"delay": 0.25, "key": "OLD00001", "name": "Old Scope"},
            {"delay": 0.0, "key": "NEW00001", "name": "New Scope"},
        ]
        await self.page.goto(f"{self.base_url}/index.html")
        tools = self.page.get_by_role("button", name="工具", exact=True)
        await tools.click()
        await self.page.wait_for_timeout(30)
        await tools.click()

        await expect(self.page.locator("#rename-collection")).to_have_value("NEW00001")
        await self.page.wait_for_timeout(300)
        await expect(self.page.locator("#rename-collection")).to_have_value("NEW00001")


if __name__ == "__main__":
    unittest.main()
