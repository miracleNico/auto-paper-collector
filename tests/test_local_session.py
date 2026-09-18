from __future__ import annotations

import unittest

import httpx


class LocalSessionMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, app, host: str = "127.0.0.1:8765") -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"http://{host}"
        )

    async def test_mutation_without_session_reports_machine_readable_expiry(self) -> None:
        from paper_endnote import app as app_module

        async with self._client(app_module.app) as client:
            response = await client.post("/api/system/shutdown")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "session_expired")
        self.assertIn("刷新", response.json()["detail"])

    async def test_root_reissues_the_cookie_that_mutations_accept(self) -> None:
        from paper_endnote import app as app_module

        previous = app_module.app.state.shutdown_callback
        app_module.app.state.shutdown_callback = None
        try:
            async with self._client(app_module.app) as client:
                await client.get("/")
                self.assertIn("paper_endnote_session", client.cookies)
                # Past the middleware: the handler itself answers (no callback -> 409).
                response = await client.post("/api/system/shutdown")
        finally:
            app_module.app.state.shutdown_callback = previous

        self.assertEqual(response.status_code, 409)

    async def test_non_local_host_is_still_rejected_as_plain_text(self) -> None:
        from paper_endnote import app as app_module

        async with self._client(app_module.app, host="example.com") as client:
            response = await client.post("/api/system/shutdown")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.text, "Local access only")


if __name__ == "__main__":
    unittest.main()
