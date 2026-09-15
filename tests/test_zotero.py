from __future__ import annotations

import unittest

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
            },
            "COLL1234",
        )
        self.assertEqual(payload["DOI"], "10.1000/example")
        self.assertEqual(payload["collections"], ["COLL1234"])
        self.assertEqual(payload["creators"][0]["lastName"], "Doe")
        self.assertEqual(payload["creators"][1]["lastName"], "Smith")

    def test_match_items_prefers_exact_doi(self) -> None:
        items = [
            {"data": {"key": "AAAA2222", "DOI": "10.1000/a", "title": "Wrong title", "date": "2024"}},
            {"data": {"key": "BBBB2222", "DOI": "10.1000/b", "title": "An Example", "date": "2024"}},
        ]
        matched = match_items(items, {"doi": "10.1000/a", "title": "An Example", "year": 2024})
        self.assertEqual(matched[0]["data"]["key"], "AAAA2222")


class ZoteroAsyncTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
