from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import unquote, urlparse

import httpx

from .inputs import normalize_doi, title_similarity
from .pdf_validation import validate_pdf


class ZoteroError(RuntimeError):
    pass


class ZoteroAuthorizationRequired(ZoteroError):
    pass


_GROUP_LIBRARY_ID = re.compile(r"group:([1-9][0-9]*)\Z")
_COLLECTION_KEY = re.compile(r"[A-Za-z0-9]{1,64}\Z")


def normalize_library_id(library_id: str = "user:0") -> str:
    """Validate and canonicalize a selectable Zotero library ID."""

    if not isinstance(library_id, str):
        raise ZoteroError(
            "无效的 Zotero library ID；仅支持 user:0 或 group:<正整数>"
        )
    if library_id == "user:0":
        return library_id
    match = _GROUP_LIBRARY_ID.fullmatch(library_id)
    if match:
        return f"group:{match.group(1)}"
    raise ZoteroError(
        "无效的 Zotero library ID；仅支持 user:0 或 group:<正整数>"
    )


def _library_path(library_id: str) -> str:
    """Return a safe Zotero API library prefix for a supported library ID."""

    normalized = normalize_library_id(library_id)
    if normalized == "user:0":
        return "/users/0"
    return f"/groups/{normalized.removeprefix('group:')}"


def _collection_path_key(collection_key: str) -> str:
    key = str(collection_key or "").strip()
    if not _COLLECTION_KEY.fullmatch(key):
        raise ZoteroError("无效的 Zotero collection key")
    return key


def _data(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("data", item)


def _creators(authors: list[str]) -> list[dict[str, str]]:
    creators: list[dict[str, str]] = []
    for author in authors:
        name = str(author).strip()
        if not name:
            continue
        if "," in name:
            last, first = (part.strip() for part in name.split(",", 1))
        else:
            parts = name.split()
            first, last = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else ("", name)
        creators.append({"creatorType": "author", "firstName": first, "lastName": last})
    return creators


def item_payload(metadata: dict[str, Any], collection_key: str) -> dict[str, Any]:
    tags = [
        {"tag": str(tag).strip()}
        for tag in metadata.get("tags") or []
        if str(tag).strip()
    ]
    if not any(item["tag"].casefold() == "paperendnote" for item in tags):
        tags.append({"tag": "PaperEndNote"})
    return {
        "itemType": "journalArticle",
        "title": metadata.get("title") or "",
        "creators": _creators(metadata.get("authors") or []),
        "abstractNote": metadata.get("abstract") or "",
        "publicationTitle": metadata.get("journal") or "",
        "volume": str(metadata.get("volume") or ""),
        "issue": str(metadata.get("issue") or ""),
        "pages": str(metadata.get("pages") or ""),
        "date": str(metadata.get("year") or ""),
        "DOI": normalize_doi(metadata.get("doi")) or "",
        "url": metadata.get("url") or "",
        "shortTitle": metadata.get("short_title") or "",
        "language": metadata.get("language") or "",
        "accessDate": metadata.get("access_date") or "",
        "libraryCatalog": metadata.get("library_catalog") or "Crossref",
        "collections": [collection_key],
        "tags": tags,
        "relations": {},
    }


def match_items(items: list[dict[str, Any]], metadata: dict[str, Any]) -> list[dict[str, Any]]:
    doi = normalize_doi(metadata.get("doi"))
    if doi:
        exact = [item for item in items if normalize_doi(_data(item).get("DOI")) == doi]
        if exact:
            return exact
    title = metadata.get("title") or ""
    year = str(metadata.get("year") or "")
    result = []
    for item in items:
        data = _data(item)
        if title_similarity(title, data.get("title")) < 0.92:
            continue
        item_year = str(data.get("date") or "")[:4]
        if year and item_year and year != item_year:
            continue
        result.append(item)
    return result


class ZoteroAdapter:
    """Zotero 10+ local API adapter with exact-match deduplication."""

    def __init__(self, api_key: str = "", base_url: str = "http://127.0.0.1:23119/api"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.server_id = ""
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=False)
        self._sync_lock = asyncio.Lock()

    @staticmethod
    def normalize_library_id(library_id: str = "user:0") -> str:
        """Public adapter entry point used by API request validation."""

        return normalize_library_id(library_id)

    @asynccontextmanager
    async def sync_guard(self) -> AsyncIterator[None]:
        """Serialize complete library syncs that share this adapter instance."""

        async with self._sync_lock:
            yield

    async def close(self) -> None:
        await self.client.aclose()

    async def probe(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "installed": Path(r"C:\Program Files\Zotero\zotero.exe").exists(),
            "running": False,
            "enabled": False,
            "authorized": bool(self.api_key),
            "ready": False,
            "version": None,
            "server_id": None,
            "details": [],
        }
        try:
            response = await self.client.get(f"{self.base_url}/", headers={"Zotero-API-Version": "3"})
            result["running"] = True
            result["version"] = response.headers.get("x-zotero-version")
            self.server_id = response.headers.get("zotero-server-id", "")
            result["server_id"] = self.server_id or None
            if response.status_code == 403 and "not enabled" in response.text.casefold():
                result["details"].append(
                    "请在 Zotero → Settings → Advanced 中启用 ‘Allow other applications on this computer to communicate with Zotero’"
                )
            else:
                response.raise_for_status()
                result["enabled"] = True
        except Exception as exc:
            result["details"].append(f"无法连接 Zotero 本地 API：{exc}")
        if result["enabled"] and not self.api_key:
            result["details"].append("尚未授予本工具 Zotero 本地写入权限")
        result["ready"] = bool(result["enabled"] and self.api_key and self.server_id)
        return result

    async def authorize(self) -> dict[str, Any]:
        probe = await self.probe()
        if not probe["enabled"]:
            raise ZoteroError("请先在 Zotero 设置的 Advanced 页面启用本地 API")
        response = await self.client.post(
            f"{self.base_url}/local/authorize",
            headers={"Content-Type": "application/json", "Zotero-Server-ID": self.server_id},
            json={"appName": "Paper EndNote Local Workflow"},
            timeout=120.0,
        )
        if response.status_code == 403:
            raise ZoteroError("Zotero 写入授权被拒绝")
        response.raise_for_status()
        payload = response.json()
        self.api_key = payload["key"]
        return {"key": self.api_key, "remember": bool(payload.get("remember"))}

    async def _ensure_server(self) -> None:
        probe = await self.probe()
        if not probe["enabled"]:
            raise ZoteroError("Zotero 本地 API 未启用")
        if not self.api_key:
            raise ZoteroAuthorizationRequired("需要先授予 Zotero 本地写入权限")

    def _headers(self, *, write: bool = False) -> dict[str, str]:
        headers = {"Zotero-API-Version": "3"}
        if self.server_id:
            headers["Zotero-Server-ID"] = self.server_id
        if write and self.api_key:
            headers["Zotero-API-Key"] = self.api_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allowed_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = {**self._headers(write=method.upper() != "GET"), **kwargs.pop("headers", {})}
        response = await self.client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        if response.status_code == 401:
            self.api_key = ""
            raise ZoteroAuthorizationRequired("Zotero 写入授权已失效，请重新授权")
        if response.status_code in (allowed_statuses or set()):
            return response
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ZoteroError(f"Zotero API {method} {path} 返回 HTTP {response.status_code}: {response.text[:300]}") from exc
        return response

    @staticmethod
    def _successful_key(response: httpx.Response) -> str:
        payload = response.json()
        successful = payload.get("successful", {})
        if not successful:
            raise ZoteroError(f"Zotero 没有创建对象：{payload}")
        first = successful[sorted(successful, key=str)[0]]
        return first.get("key") or first.get("data", {}).get("key")

    async def list_libraries(self) -> list[dict[str, Any]]:
        """List the personal library and locally available group libraries."""

        await self._ensure_server()
        result: list[dict[str, Any]] = [
            {
                "id": "user:0",
                "library_id": "user:0",
                "name": "My Library",
                "type": "user",
            }
        ]
        groups: list[dict[str, Any]] = []
        start = 0
        while True:
            response = await self._request(
                "GET",
                "/users/0/groups",
                allowed_statuses={404, 501},
                params={"limit": 100, "start": start},
            )
            if response.status_code in {404, 501}:
                return result
            page = response.json()
            groups.extend(page)
            if len(page) < 100:
                break
            start += 100
        for item in groups:
            data = _data(item)
            group_id = data.get("id", item.get("id"))
            try:
                numeric_id = int(group_id)
            except (TypeError, ValueError):
                continue
            if numeric_id <= 0:
                continue
            library_id = f"group:{numeric_id}"
            result.append(
                {
                    "id": library_id,
                    "library_id": library_id,
                    "name": data.get("name") or f"Group {numeric_id}",
                    "type": "group",
                    "group_id": numeric_id,
                }
            )
        return result

    async def ensure_collection(
        self, name: str, *, create: bool, library_id: str = "user:0"
    ) -> str:
        library_path = _library_path(library_id)
        await self._ensure_server()
        response = await self._request("GET", f"{library_path}/collections")
        matches = [item for item in response.json() if _data(item).get("name", "").casefold() == name.casefold()]
        if len(matches) == 1:
            return _data(matches[0]).get("key") or matches[0].get("key")
        if len(matches) > 1:
            raise ZoteroError(f"存在多个同名 Zotero collection：{name}")
        if not create:
            raise ZoteroError(f"Zotero collection 不存在：{name}")
        response = await self._request(
            "POST",
            f"{library_path}/collections",
            json=[{"name": name, "parentCollection": False, "relations": {}}],
            headers={"Zotero-Write-Token": secrets.token_hex(16)},
        )
        return self._successful_key(response)

    async def _candidate_items(
        self, metadata: dict[str, Any], library_id: str = "user:0"
    ) -> list[dict[str, Any]]:
        library_path = _library_path(library_id)
        doi = normalize_doi(metadata.get("doi"))
        title = str(metadata.get("title") or "").strip()
        if doi:
            response = await self._request(
                "GET", f"{library_path}/items/top", params={"q": doi, "qmode": "everything"}
            )
            exact = [
                item
                for item in response.json()
                if normalize_doi(_data(item).get("DOI")) == doi
            ]
            if exact or not title:
                return exact

            response = await self._request(
                "GET", f"{library_path}/items/top", params={"q": title, "qmode": "everything"}
            )
            title_matches = match_items(
                response.json(), {**metadata, "doi": None}
            )
            return [
                item
                for item in title_matches
                if not normalize_doi(_data(item).get("DOI"))
                or normalize_doi(_data(item).get("DOI")) == doi
            ]

        response = await self._request(
            "GET", f"{library_path}/items/top", params={"q": title, "qmode": "everything"}
        )
        return match_items(response.json(), metadata)

    async def _add_to_collection(
        self, item: dict[str, Any], collection_key: str, library_id: str = "user:0"
    ) -> None:
        data = _data(item)
        collections = list(data.get("collections") or [])
        if collection_key in collections:
            return
        collections.append(collection_key)
        await self._request(
            "PATCH",
            f"{_library_path(library_id)}/items/{data.get('key') or item.get('key')}",
            json={"version": data.get("version", item.get("version")), "collections": collections},
        )

    async def _children(
        self, item_key: str, library_id: str = "user:0"
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", f"{_library_path(library_id)}/items/{item_key}/children"
        )
        return response.json()

    async def _attachment_path(
        self, attachment_key: str, library_id: str = "user:0"
    ) -> Path | None:
        response = await self.client.get(
            f"{self.base_url}{_library_path(library_id)}/items/{attachment_key}/file/view/url",
            headers=self._headers(),
        )
        if response.status_code != 200:
            return None
        value = response.text.strip()
        parsed = urlparse(value)
        if parsed.scheme != "file":
            return None
        path = unquote(parsed.path)
        if path.startswith("/") and len(path) > 3 and path[2] == ":":
            path = path[1:]
        return Path(path)

    async def _existing_main_pdf(
        self,
        item_key: str,
        metadata: dict[str, Any],
        library_id: str = "user:0",
    ) -> tuple[bool, list[dict[str, Any]]]:
        children = await self._children(item_key, library_id)
        pdfs = [item for item in children if _data(item).get("contentType") == "application/pdf"]
        for item in pdfs:
            key = _data(item).get("key") or item.get("key")
            path = await self._attachment_path(key, library_id)
            if path and path.is_file():
                result = validate_pdf(path, expected_doi=metadata.get("doi"), expected_title=metadata.get("title"))
                if result.valid_pdf and result.identity == "verified" and result.role == "main":
                    return True, pdfs
        return False, pdfs

    async def _create_attachment(
        self, item_key: str, pdf_path: Path, library_id: str = "user:0"
    ) -> str:
        payload = {
            "itemType": "attachment",
            "parentItem": item_key,
            "linkMode": "imported_file",
            "title": pdf_path.name,
            "accessDate": "",
            "url": "",
            "note": "",
            "tags": [],
            "relations": {},
            "contentType": "application/pdf",
            "charset": "",
            "filename": pdf_path.name,
        }
        response = await self._request(
            "POST",
            f"{_library_path(library_id)}/items",
            json=[payload],
            headers={"Zotero-Write-Token": secrets.token_hex(16)},
        )
        return self._successful_key(response)

    async def _upload_file(
        self, attachment_key: str, pdf_path: Path, library_id: str = "user:0"
    ) -> None:
        digest = hashlib.md5(pdf_path.read_bytes(), usedforsecurity=False).hexdigest()
        form = {
            "md5": digest,
            "filename": pdf_path.name,
            "filesize": str(pdf_path.stat().st_size),
            "mtime": str(int(pdf_path.stat().st_mtime * 1000)),
        }
        condition = {"If-None-Match": "*"}
        response = await self._request(
            "POST",
            f"{_library_path(library_id)}/items/{attachment_key}/file",
            data=form,
            headers=condition,
        )
        authorization = response.json()
        if not authorization.get("exists"):
            body = authorization.get("prefix", "").encode() + pdf_path.read_bytes() + authorization.get("suffix", "").encode()
            upload = await self.client.post(
                authorization["url"], content=body, headers={"Content-Type": authorization["contentType"]}
            )
            if upload.status_code != 201:
                raise ZoteroError(f"Zotero PDF 上传失败：HTTP {upload.status_code}")
            await self._request(
                "POST",
                f"{_library_path(library_id)}/items/{attachment_key}/file",
                data={"upload": authorization["uploadKey"]},
                headers=condition,
            )

    async def attach_pdf(
        self, item_key: str, pdf_path: Path, library_id: str = "user:0"
    ) -> dict[str, Any]:
        """Attach one local PDF unless the same bytes are already attached."""

        _library_path(library_id)
        await self._ensure_server()
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise ZoteroError(f"PDF 不存在：{pdf_path}")
        wanted_md5 = hashlib.md5(pdf_path.read_bytes(), usedforsecurity=False).hexdigest()
        children = await self._children(item_key, library_id)
        incomplete_key = None
        for child in children:
            data = _data(child)
            if data.get("contentType") != "application/pdf":
                continue
            key = data.get("key") or child.get("key")
            if data.get("md5") == wanted_md5:
                return {"attached": False, "attachment_key": key, "existing": True}
            if key and not data.get("md5"):
                existing_path = await self._attachment_path(key, library_id)
                if existing_path and existing_path.is_file():
                    existing_md5 = hashlib.md5(
                        existing_path.read_bytes(), usedforsecurity=False
                    ).hexdigest()
                    if existing_md5 == wanted_md5:
                        return {"attached": False, "attachment_key": key, "existing": True}
                    continue
                if (
                    incomplete_key is None
                    and data.get("filename") == pdf_path.name
                    and data.get("linkMode") in {None, "imported_file", "imported_url"}
                ):
                    incomplete_key = key
        attachment_key = incomplete_key or await self._create_attachment(
            item_key, pdf_path, library_id
        )
        await self._upload_file(attachment_key, pdf_path, library_id)
        final_children = await self._children(item_key, library_id)
        if not any(_data(item).get("md5") == wanted_md5 for item in final_children):
            raise ZoteroError("Zotero 写入后未找到匹配的 PDF 附件")
        return {"attached": True, "attachment_key": attachment_key, "existing": False}

    async def commit_paper(
        self,
        collection_key: str,
        metadata: dict[str, Any],
        pdf_path: Path | None,
        library_id: str = "user:0",
    ) -> dict[str, Any]:
        library_path = _library_path(library_id)
        await self._ensure_server()
        matches = await self._candidate_items(metadata, library_id)
        if len(matches) > 1:
            raise ZoteroError("Zotero 中有多个匹配题录，需要人工处理")
        created = False
        if matches:
            item = matches[0]
            await self._add_to_collection(item, collection_key, library_id)
            item_key = _data(item).get("key") or item.get("key")
        else:
            response = await self._request(
                "POST",
                f"{library_path}/items",
                json=[item_payload(metadata, collection_key)],
                headers={"Zotero-Write-Token": secrets.token_hex(16)},
            )
            item_key = self._successful_key(response)
            created = True

        existing_main, children = await self._existing_main_pdf(
            item_key, metadata, library_id
        )
        attached = False
        attachment_key = None
        if pdf_path and not existing_main:
            wanted_md5 = hashlib.md5(pdf_path.read_bytes(), usedforsecurity=False).hexdigest()
            exact = [item for item in children if _data(item).get("md5") == wanted_md5]
            if not exact:
                incomplete = [
                    item for item in children
                    if _data(item).get("filename") == pdf_path.name and not _data(item).get("md5")
                ]
                attachment_key = (
                    (_data(incomplete[0]).get("key") or incomplete[0].get("key"))
                    if incomplete else await self._create_attachment(item_key, pdf_path, library_id)
                )
                await self._upload_file(attachment_key, pdf_path, library_id)
                attached = True

        final = (await self._request("GET", f"{library_path}/items/{item_key}")).json()
        if len(match_items([final], metadata)) != 1:
            raise ZoteroError("Zotero 写入后题录核验失败")
        final_children = await self._children(item_key, library_id)
        if pdf_path and not existing_main:
            wanted_md5 = hashlib.md5(pdf_path.read_bytes(), usedforsecurity=False).hexdigest()
            if not any(_data(item).get("md5") == wanted_md5 for item in final_children):
                raise ZoteroError("Zotero 写入后未找到匹配的 PDF 附件")
        return {
            "created": created,
            "attached": attached,
            "existing_full_text": existing_main,
            "record_number": item_key,
            "attachment_key": attachment_key,
            "collection_key": collection_key,
        }

    @staticmethod
    def metadata_from_item(item: dict[str, Any]) -> dict[str, Any]:
        data = _data(item)
        authors: list[str] = []
        for creator in data.get("creators") or []:
            if (creator.get("creatorType") or "author") != "author":
                continue
            if creator.get("name"):
                authors.append(str(creator["name"]).strip())
            elif creator.get("lastName"):
                family = str(creator.get("lastName") or "").strip()
                given = str(creator.get("firstName") or "").strip()
                authors.append(", ".join(part for part in (family, given) if part))
        date = str(data.get("date") or "")
        year_match = date[:4] if date[:4].isdigit() else None
        return {
            "doi": normalize_doi(data.get("DOI")),
            "title": data.get("title") or "",
            "year": int(year_match) if year_match else None,
            "authors": authors,
            "journal": data.get("publicationTitle") or "",
            "volume": data.get("volume") or "",
            "issue": data.get("issue") or "",
            "pages": data.get("pages") or "",
            "url": data.get("url") or "",
            "abstract": data.get("abstractNote") or "",
            "type": "journal-article",
        }

    async def export_row(
        self,
        item_key: str,
        fallback_metadata: dict[str, Any] | None = None,
        library_id: str = "user:0",
    ) -> dict[str, Any]:
        library_path = _library_path(library_id)
        await self._ensure_server()
        item = (await self._request("GET", f"{library_path}/items/{item_key}")).json()
        metadata = self.metadata_from_item(item)
        if fallback_metadata:
            for key in ("doi", "title", "year", "authors", "journal", "volume", "issue", "pages", "url"):
                if not metadata.get(key) and fallback_metadata.get(key):
                    metadata[key] = fallback_metadata[key]
        children = await self._children(item_key, library_id)
        pdf_path = None
        attachment_key = None
        attachment_version = None
        for child in children:
            data = _data(child)
            if data.get("contentType") != "application/pdf":
                continue
            key = data.get("key") or child.get("key")
            path = await self._attachment_path(key, library_id)
            if path and path.is_file():
                pdf_path = path
                attachment_key = key
                attachment_version = data.get("version", child.get("version"))
                result = validate_pdf(
                    path, expected_doi=metadata.get("doi"), expected_title=metadata.get("title")
                )
                if result.valid_pdf and result.identity == "verified" and result.role == "main":
                    break
        return {
            "zotero_key": item_key,
            "attachment_key": attachment_key,
            "attachment_version": attachment_version,
            "metadata": metadata,
            "pdf_path": pdf_path,
        }

    async def list_collections(
        self, library_id: str = "user:0"
    ) -> list[dict[str, Any]]:
        library_path = _library_path(library_id)
        await self._ensure_server()
        items: list[dict[str, Any]] = []
        start = 0
        while True:
            page = (
                await self._request(
                    "GET",
                    f"{library_path}/collections",
                    params={"limit": 100, "start": start},
                )
            ).json()
            items.extend(page)
            if len(page) < 100:
                break
            start += 100
        result: list[dict[str, Any]] = []
        for item in items:
            data = _data(item)
            result.append(
                {
                    "key": data.get("key") or item.get("key"),
                    "name": data.get("name") or "",
                    "parentCollection": data.get("parentCollection") or "",
                    "numItems": (item.get("meta") or {}).get("numItems"),
                }
            )

        by_key = {str(row["key"]): row for row in result if row.get("key")}
        paths: dict[str, str] = {}

        def collection_path(key: str, trail: frozenset[str] = frozenset()) -> str:
            if key in paths:
                return paths[key]
            row = by_key[key]
            name = str(row.get("name") or key)
            parent = str(row.get("parentCollection") or "")
            if key in trail:
                return name
            if parent and parent in by_key and parent not in trail:
                path = f"{collection_path(parent, trail | {key})} / {name}"
            else:
                path = name
            paths[key] = path
            return path

        for key in by_key:
            collection_path(key)
        path_counts: dict[str, int] = {}
        for path in paths.values():
            path_counts[path] = path_counts.get(path, 0) + 1
        for row in result:
            key = str(row.get("key") or "")
            path = paths.get(key, str(row.get("name") or key))
            row["path"] = f"{path} [{key}]" if path_counts.get(path, 0) > 1 else path
        return result

    async def iter_library_items(
        self, library_id: str = "user:0", collection_key: str = ""
    ):
        """Iterate top-level items in a whole library or one collection."""

        library_path = _library_path(library_id)
        key = _collection_path_key(collection_key) if collection_key else ""
        await self._ensure_server()
        if key:
            path = f"{library_path}/collections/{key}/items/top"
        else:
            path = f"{library_path}/items/top"
        start = 0
        while True:
            response = await self._request(
                "GET",
                path,
                params={"limit": 100, "start": start},
            )
            rows = response.json()
            if not rows:
                break
            for item in rows:
                yield item
            if len(rows) < 100:
                break
            start += 100

    async def iter_collection_items(
        self, name: str, library_id: str = "user:0"
    ):
        """Backward-compatible name-based collection iterator."""

        key = await self.ensure_collection(
            name, create=False, library_id=library_id
        )
        async for item in self.iter_library_items(library_id, key):
            yield item

    async def rename_attachment_filename(
        self,
        attachment_key: str,
        filename: str,
        version: int | None,
        library_id: str = "user:0",
    ) -> None:
        payload: dict[str, Any] = {"filename": filename, "title": filename}
        if version is not None:
            payload["version"] = version
        await self._request(
            "PATCH",
            f"{_library_path(library_id)}/items/{attachment_key}",
            json=payload,
        )

    async def attachment_filename(
        self, attachment_key: str, library_id: str = "user:0"
    ) -> str:
        """Read the filename currently committed for one attachment."""

        response = await self._request(
            "GET",
            f"{_library_path(library_id)}/items/{attachment_key}",
        )
        data = _data(response.json())
        return str(data.get("filename") or data.get("title") or "")
