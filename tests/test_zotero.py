from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx

from paper_endnote.zotero import (
    ZoteroAdapter,
    ZoteroError,
    item_payload,
    match_items,
    normalize_library_id,
)


class ZoteroTests(unittest.TestCase):
    def test_normalize_library_id_accepts_only_supported_forms(self) -> None:
        self.assertEqual(normalize_library_id(), "user:0")
        self.assertEqual(normalize_library_id("group:42"), "group:42")
        for library_id in ("group:0", "group:01", "group:42/items", None):
            with self.subTest(library_id=library_id):
                with self.assertRaises(ZoteroError):
                    normalize_library_id(library_id)  # type: ignore[arg-type]

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
    async def test_list_libraries_includes_personal_and_group_libraries(self) -> None:
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            headers = {
                "Zotero-Server-ID": "SERVER123",
                "Zotero-API-Version": "3",
                "X-Zotero-Version": "10.0.2",
            }
            if request.url.path == "/api/":
                return httpx.Response(200, headers=headers, json={})
            if request.url.path == "/api/users/0/groups":
                return httpx.Response(
                    200,
                    headers=headers,
                    json=[{"data": {"id": 42, "name": "DTN Group"}}],
                )
            return httpx.Response(404, headers=headers, text=request.url.path)

        adapter = ZoteroAdapter(api_key="test-key")
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            libraries = await adapter.list_libraries()
        finally:
            await adapter.close()

        self.assertEqual(libraries[0]["id"], "user:0")
        self.assertEqual(libraries[0]["type"], "user")
        self.assertEqual(libraries[1]["id"], "group:42")
        self.assertEqual(libraries[1]["library_id"], "group:42")
        self.assertEqual(libraries[1]["name"], "DTN Group")
        self.assertEqual(
            requests,
            [("GET", "/api/"), ("GET", "/api/users/0/groups")],
        )

    async def test_list_libraries_falls_back_when_group_endpoint_is_unsupported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            headers = {
                "Zotero-Server-ID": "SERVER123",
                "Zotero-API-Version": "3",
                "X-Zotero-Version": "10.0.2",
            }
            if request.url.path == "/api/":
                return httpx.Response(200, headers=headers, json={})
            if request.url.path == "/api/users/0/groups":
                return httpx.Response(501, headers=headers)
            return httpx.Response(404, headers=headers)

        adapter = ZoteroAdapter(api_key="test-key")
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            libraries = await adapter.list_libraries()
        finally:
            await adapter.close()

        self.assertEqual(
            libraries,
            [
                {
                    "id": "user:0",
                    "library_id": "user:0",
                    "name": "My Library",
                    "type": "user",
                }
            ],
        )

    async def test_group_library_collections_and_item_iteration_use_group_paths(self) -> None:
        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            headers = {
                "Zotero-Server-ID": "SERVER123",
                "Zotero-API-Version": "3",
                "X-Zotero-Version": "10.0.2",
            }
            path = request.url.path
            if path == "/api/":
                return httpx.Response(200, headers=headers, json={})
            if path == "/api/groups/42/collections":
                return httpx.Response(
                    200,
                    headers=headers,
                    json=[
                        {
                            "data": {"key": "COLL1234", "name": "DTN"},
                            "meta": {"numItems": 1},
                        }
                    ],
                )
            if path == "/api/groups/42/items/top":
                return httpx.Response(
                    200, headers=headers, json=[{"data": {"key": "ALL12345"}}]
                )
            if path == "/api/groups/42/collections/COLL1234/items/top":
                return httpx.Response(
                    200, headers=headers, json=[{"data": {"key": "ONE12345"}}]
                )
            return httpx.Response(404, headers=headers, text=path)

        adapter = ZoteroAdapter(api_key="test-key")
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            collections = await adapter.list_collections("group:42")
            all_items = [
                item async for item in adapter.iter_library_items("group:42")
            ]
            collection_items = [
                item
                async for item in adapter.iter_library_items(
                    "group:42", "COLL1234"
                )
            ]
        finally:
            await adapter.close()

        self.assertEqual(
            collections,
            [
                {
                    "key": "COLL1234",
                    "name": "DTN",
                    "parentCollection": "",
                    "numItems": 1,
                    "path": "DTN",
                }
            ],
        )
        self.assertEqual(all_items[0]["data"]["key"], "ALL12345")
        self.assertEqual(collection_items[0]["data"]["key"], "ONE12345")
        self.assertIn(("GET", "/api/groups/42/items/top"), requests)
        self.assertIn(
            ("GET", "/api/groups/42/collections/COLL1234/items/top"),
            requests,
        )

    async def test_collection_paths_distinguish_nested_duplicate_names(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            headers = {
                "Zotero-Server-ID": "SERVER123",
                "Zotero-API-Version": "3",
                "X-Zotero-Version": "10.0.2",
            }
            if request.url.path == "/api/":
                return httpx.Response(200, headers=headers, json={})
            if request.url.path == "/api/users/0/collections":
                return httpx.Response(
                    200,
                    headers=headers,
                    json=[
                        {
                            "data": {
                                "key": "ROOT0001",
                                "name": "Project A",
                                "parentCollection": False,
                            },
                            "meta": {"numItems": 1},
                        },
                        {
                            "data": {
                                "key": "ROOT0002",
                                "name": "Project B",
                                "parentCollection": False,
                            },
                            "meta": {"numItems": 1},
                        },
                        {
                            "data": {
                                "key": "CHILD001",
                                "name": "Papers",
                                "parentCollection": "ROOT0001",
                            },
                            "meta": {"numItems": 2},
                        },
                        {
                            "data": {
                                "key": "CHILD002",
                                "name": "Papers",
                                "parentCollection": "ROOT0002",
                            },
                            "meta": {"numItems": 3},
                        },
                    ],
                )
            return httpx.Response(404, headers=headers)

        adapter = ZoteroAdapter(api_key="test-key")
        await adapter.client.aclose()
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            collections = await adapter.list_collections()
        finally:
            await adapter.close()

        paths = {item["key"]: item["path"] for item in collections}
        self.assertEqual(paths["CHILD001"], "Project A / Papers")
        self.assertEqual(paths["CHILD002"], "Project B / Papers")

    async def test_group_export_and_rename_resolve_attachments_in_group(self) -> None:
        requests: list[tuple[str, str]] = []
        pdf_path = None

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            headers = {
                "Zotero-Server-ID": "SERVER123",
                "Zotero-API-Version": "3",
                "X-Zotero-Version": "10.0.2",
            }
            path = request.url.path
            if path == "/api/":
                return httpx.Response(200, headers=headers, json={})
            if path == "/api/groups/42/items/ITEM1234":
                return httpx.Response(
                    200,
                    headers=headers,
                    json={
                        "data": {
                            "key": "ITEM1234",
                            "title": "Group Paper",
                            "date": "2024",
                        }
                    },
                )
            if path == "/api/groups/42/items/ITEM1234/children":
                return httpx.Response(
                    200,
                    headers=headers,
                    json=[
                        {
                            "data": {
                                "key": "FILE1234",
                                "version": 7,
                                "contentType": "application/pdf",
                            }
                        }
                    ],
                )
            if path == "/api/groups/42/items/FILE1234/file/view/url":
                return httpx.Response(200, headers=headers, text=pdf_path.as_uri())
            if path == "/api/groups/42/items/FILE1234" and request.method == "GET":
                return httpx.Response(
                    200,
                    headers=headers,
                    json={"data": {"key": "FILE1234", "filename": "renamed.pdf"}},
                )
            if path == "/api/groups/42/items/FILE1234" and request.method == "PATCH":
                return httpx.Response(204, headers=headers)
            return httpx.Response(404, headers=headers, text=path)

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "paper.pdf"
            pdf_path.write_bytes(
                b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
            )
            adapter = ZoteroAdapter(api_key="test-key")
            await adapter.client.aclose()
            adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                row = await adapter.export_row(
                    "ITEM1234", library_id="group:42"
                )
                await adapter.rename_attachment_filename(
                    "FILE1234", "renamed.pdf", 7, library_id="group:42"
                )
                filename = await adapter.attachment_filename(
                    "FILE1234", library_id="group:42"
                )
            finally:
                await adapter.close()

        self.assertEqual(row["attachment_key"], "FILE1234")
        self.assertEqual(row["pdf_path"], pdf_path)
        self.assertEqual(filename, "renamed.pdf")
        self.assertIn(
            ("GET", "/api/groups/42/items/FILE1234/file/view/url"), requests
        )
        self.assertIn(("PATCH", "/api/groups/42/items/FILE1234"), requests)

    async def test_invalid_library_ids_are_rejected_before_network_request(self) -> None:
        adapter = ZoteroAdapter(api_key="test-key")
        try:
            for library_id in (
                "",
                "user:1",
                "group:0",
                "group:-1",
                "group:01",
                "group:42/items",
                "group:42?x=1",
            ):
                with self.subTest(library_id=library_id):
                    with self.assertRaisesRegex(ZoteroError, "library ID"):
                        await adapter.list_collections(library_id)
        finally:
            await adapter.close()

    async def test_invalid_collection_key_is_rejected_before_network_request(self) -> None:
        adapter = ZoteroAdapter(api_key="test-key")
        try:
            with self.assertRaisesRegex(ZoteroError, "collection key"):
                _ = [
                    item
                    async for item in adapter.iter_library_items(
                        "group:42", "COLL/../../items"
                    )
                ]
        finally:
            await adapter.close()

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
