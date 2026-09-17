from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx

from paper_endnote.zotero import ZoteroAdapter, item_payload, match_items


class ZoteroTests(unittest.TestCase):
    def test_item_payload_and_creator_conversion(self) -> None:
        payload = item_payload(
            {
                "doi": "https://doi.org/10.1000/Example",
                "title": "An Example",
                "year": 2024,
                "authors": ["Doe, Jane", "John Smith"],
                "journal": "A Journal",
                "library_catalog": "EndNote",
                "tags": ["EndNote import", "reviewed"],
            },
            "COLL1234",
        )
        self.assertEqual(payload["DOI"], "10.1000/example")
        self.assertEqual(payload["collections"], ["COLL1234"])
        self.assertEqual(payload["creators"][0]["lastName"], "Doe")
        self.assertEqual(payload["creators"][1]["lastName"], "Smith")
        self.assertEqual(payload["libraryCatalog"], "EndNote")
        self.assertEqual(
            payload["tags"],
            [
                {"tag": "EndNote import"},
                {"tag": "reviewed"},
                {"tag": "PaperEndNote"},
            ],
        )

    def test_match_items_prefers_exact_doi(self) -> None:
        items = [
            {"data": {"key": "AAAA2222", "DOI": "10.1000/a", "title": "Wrong title", "date": "2024"}},
            {"data": {"key": "BBBB2222", "DOI": "10.1000/b", "title": "An Example", "date": "2024"}},
        ]
        matched = match_items(items, {"doi": "10.1000/a", "title": "An Example", "year": 2024})
        self.assertEqual(matched[0]["data"]["key"], "AAAA2222")


class ZoteroAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_items_falls_back_to_title_year_after_doi_miss(self) -> None:
        queries: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params.get("q", "")
            queries.append(query)
            if query == "10.1000/example":
                return httpx.Response(200, json=[])
            if query == "An Example":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "data": {
                                "key": "NO_DOI",
                                "DOI": "",
                                "title": "An Example",
                                "date": "2024",
                            }
                        },
                        {
                            "data": {
                                "key": "OTHER_DOI",
                                "DOI": "10.1000/other",
                                "title": "An Example",
                                "date": "2024",
                            }
                        },
                    ],
                )
            return httpx.Response(500, text="unexpected query")

        adapter = ZoteroAdapter(api_key="test-key")
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            matches = await adapter._candidate_items(
                {"doi": "10.1000/example", "title": "An Example", "year": 2024}
            )
        finally:
            await adapter.close()

        self.assertEqual(queries, ["10.1000/example", "An Example"])
        self.assertEqual([item["data"]["key"] for item in matches], ["NO_DOI"])

    async def test_attach_pdf_reuses_child_with_same_md5(self) -> None:
        requests: list[tuple[str, str]] = []

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nmatching bytes\n%%EOF\n")
            digest = hashlib.md5(pdf_path.read_bytes(), usedforsecurity=False).hexdigest()

            def handler(request: httpx.Request) -> httpx.Response:
                requests.append((request.method, request.url.path))
                headers = {
                    "Zotero-Server-ID": "SERVER123",
                    "Zotero-API-Version": "3",
                    "X-Zotero-Version": "10.0.2",
                }
                if request.url.path == "/api/":
                    return httpx.Response(200, headers=headers, json={})
                if request.url.path == "/api/users/0/items/ITEM1234/children":
                    return httpx.Response(
                        200,
                        headers=headers,
                        json=[
                            {
                                "data": {
                                    "key": "FILE1234",
                                    "contentType": "application/pdf",
                                    "md5": digest,
                                }
                            }
                        ],
                    )
                return httpx.Response(500, headers=headers, text="unexpected request")

            adapter = ZoteroAdapter(api_key="test-key")
            await adapter.client.aclose()
            adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                result = await adapter.attach_pdf("ITEM1234", pdf_path)
            finally:
                await adapter.close()

        self.assertEqual(
            result,
            {"attached": False, "attachment_key": "FILE1234", "existing": True},
        )
        self.assertEqual(
            requests,
            [
                ("GET", "/api/"),
                ("GET", "/api/users/0/items/ITEM1234/children"),
            ],
        )

    async def test_attach_pdf_reuses_incomplete_same_name_attachment(self) -> None:
        requests: list[tuple[str, str]] = []
        children_requests = 0

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nretry bytes\n%%EOF\n")
            digest = hashlib.md5(pdf_path.read_bytes(), usedforsecurity=False).hexdigest()

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal children_requests
                requests.append((request.method, request.url.path))
                headers = {
                    "Zotero-Server-ID": "SERVER123",
                    "Zotero-API-Version": "3",
                    "X-Zotero-Version": "10.0.2",
                }
                if request.url.path == "/api/":
                    return httpx.Response(200, headers=headers, json={})
                if request.url.path == "/api/users/0/items/ITEM1234/children":
                    children_requests += 1
                    md5 = None if children_requests == 1 else digest
                    return httpx.Response(
                        200,
                        headers=headers,
                        json=[
                            {
                                "data": {
                                    "key": "FILE1234",
                                    "contentType": "application/pdf",
                                    "filename": "paper.pdf",
                                    "linkMode": "imported_file",
                                    "md5": md5,
                                }
                            }
                        ],
                    )
                if request.url.path == "/api/users/0/items/FILE1234/file/view/url":
                    return httpx.Response(404, headers=headers)
                if (
                    request.url.path == "/api/users/0/items/FILE1234/file"
                    and request.method == "POST"
                ):
                    return httpx.Response(200, headers=headers, json={"exists": 1})
                return httpx.Response(500, headers=headers, text="unexpected request")

            adapter = ZoteroAdapter(api_key="test-key")
            await adapter.client.aclose()
            adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                result = await adapter.attach_pdf("ITEM1234", pdf_path)
            finally:
                await adapter.close()

        self.assertEqual(
            result,
            {"attached": True, "attachment_key": "FILE1234", "existing": False},
        )
        self.assertNotIn(("POST", "/api/users/0/items"), requests)
        self.assertEqual(
            requests,
            [
                ("GET", "/api/"),
                ("GET", "/api/users/0/items/ITEM1234/children"),
                ("GET", "/api/users/0/items/FILE1234/file/view/url"),
                ("POST", "/api/users/0/items/FILE1234/file"),
                ("GET", "/api/users/0/items/ITEM1234/children"),
            ],
        )

    async def test_create_collection_and_metadata_only_item(self) -> None:
        created_items = []

        def handler(request: httpx.Request) -> httpx.Response:
            headers = {
                "Zotero-Server-ID": "SERVER123",
                "Zotero-API-Version": "3",
                "X-Zotero-Version": "10.0.2",
            }
            path = request.url.path
            if path == "/api/":
                return httpx.Response(200, headers=headers, json={})
            if path == "/api/users/0/collections" and request.method == "GET":
                return httpx.Response(200, headers=headers, json=[])
            if path == "/api/users/0/collections" and request.method == "POST":
                return httpx.Response(200, headers=headers, json={"successful": {"0": {"key": "COLL1234"}}})
            if path == "/api/users/0/items/top":
                return httpx.Response(200, headers=headers, json=[])
            if path == "/api/users/0/items" and request.method == "POST":
                created_items.append(request.content.decode())
                return httpx.Response(200, headers=headers, json={"successful": {"0": {"key": "ITEM1234"}}})
            if path == "/api/users/0/items/ITEM1234/children":
                return httpx.Response(200, headers=headers, json=[])
            if path == "/api/users/0/items/ITEM1234":
                return httpx.Response(
                    200,
                    headers=headers,
                    json={"data": {"key": "ITEM1234", "DOI": "10.1000/example", "title": "An Example", "date": "2024"}},
                )
            return httpx.Response(404, headers=headers, text=path)

        adapter = ZoteroAdapter(api_key="test-key")
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            collection = await adapter.ensure_collection("DTN", create=True)
            result = await adapter.commit_paper(
                collection,
                {"doi": "10.1000/example", "title": "An Example", "year": 2024, "authors": []},
                None,
            )
        finally:
            await adapter.close()
        self.assertEqual(collection, "COLL1234")
        self.assertEqual(result["record_number"], "ITEM1234")
        self.assertTrue(result["created"])
        self.assertIn('"collections":["COLL1234"]', created_items[0].replace(" ", ""))

    async def test_export_row_reads_zotero_file_url(self) -> None:
        pdf_path = None

        def handler(request: httpx.Request) -> httpx.Response:
            headers = {
                "Zotero-Server-ID": "SERVER123",
                "Zotero-API-Version": "3",
                "X-Zotero-Version": "10.0.2",
            }
            path = request.url.path
            if path == "/api/":
                return httpx.Response(200, headers=headers, json={})
            if path == "/api/users/0/items/ITEM1234":
                return httpx.Response(
                    200,
                    headers=headers,
                    json={
                        "data": {
                            "key": "ITEM1234",
                            "DOI": "10.1000/example",
                            "title": "An Example",
                            "date": "2024",
                            "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
                        }
                    },
                )
            if path == "/api/users/0/items/ITEM1234/children":
                return httpx.Response(
                    200,
                    headers=headers,
                    json=[{"data": {"key": "FILE1234", "contentType": "application/pdf"}}],
                )
            if path == "/api/users/0/items/FILE1234/file/view/url":
                return httpx.Response(200, headers=headers, text=pdf_path.as_uri())
            return httpx.Response(404, headers=headers, text=path)

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
            adapter = ZoteroAdapter(api_key="test-key")
            await adapter.client.aclose()
            adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                row = await adapter.export_row("ITEM1234")
            finally:
                await adapter.close()
        self.assertEqual(row["zotero_key"], "ITEM1234")
        self.assertEqual(row["attachment_key"], "FILE1234")
        self.assertEqual(row["metadata"]["title"], "An Example")
        self.assertEqual(row["pdf_path"], pdf_path)


if __name__ == "__main__":
    unittest.main()
