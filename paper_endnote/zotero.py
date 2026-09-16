from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from .inputs import normalize_doi, title_similarity
from .pdf_validation import validate_pdf


class ZoteroError(RuntimeError):
    pass


class ZoteroAuthorizationRequired(ZoteroError):
    pass


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
        "libraryCatalog": "Crossref",
        "collections": [collection_key],
        "tags": [{"tag": "PaperEndNote"}],
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

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers(write=method.upper() != "GET"), **kwargs.pop("headers", {})}
        response = await self.client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        if response.status_code == 401:
            self.api_key = ""
            raise ZoteroAuthorizationRequired("Zotero 写入授权已失效，请重新授权")
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

    async def ensure_collection(self, name: str, *, create: bool) -> str:
        await self._ensure_server()
        response = await self._request("GET", "/users/0/collections")
        matches = [item for item in response.json() if _data(item).get("name", "").casefold() == name.casefold()]
        if len(matches) == 1:
            return _data(matches[0]).get("key") or matches[0].get("key")
        if len(matches) > 1:
            raise ZoteroError(f"存在多个同名 Zotero collection：{name}")
        if not create:
            raise ZoteroError(f"Zotero collection 不存在：{name}")
        response = await self._request(
            "POST",
            "/users/0/collections",
            json=[{"name": name, "parentCollection": False, "relations": {}}],
            headers={"Zotero-Write-Token": secrets.token_hex(16)},
        )
        return self._successful_key(response)

    async def _candidate_items(self, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        query = normalize_doi(metadata.get("doi")) or metadata.get("title") or ""
        response = await self._request(
            "GET", "/users/0/items/top", params={"q": query, "qmode": "everything"}
        )
        return match_items(response.json(), metadata)

    async def _add_to_collection(self, item: dict[str, Any], collection_key: str) -> None:
        data = _data(item)
        collections = list(data.get("collections") or [])
        if collection_key in collections:
            return
        collections.append(collection_key)
        await self._request(
            "PATCH",
            f"/users/0/items/{data.get('key') or item.get('key')}",
            json={"version": data.get("version", item.get("version")), "collections": collections},
        )

    async def _children(self, item_key: str) -> list[dict[str, Any]]:
        response = await self._request("GET", f"/users/0/items/{item_key}/children")
        return response.json()

    async def _attachment_path(self, attachment_key: str) -> Path | None:
        response = await self.client.get(
            f"{self.base_url}/users/0/items/{attachment_key}/file/view/url",
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

    async def _existing_main_pdf(self, item_key: str, metadata: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
        children = await self._children(item_key)
        pdfs = [item for item in children if _data(item).get("contentType") == "application/pdf"]
        for item in pdfs:
            key = _data(item).get("key") or item.get("key")
            path = await self._attachment_path(key)
            if path and path.is_file():
                result = validate_pdf(path, expected_doi=metadata.get("doi"), expected_title=metadata.get("title"))
                if result.valid_pdf and result.identity == "verified" and result.role == "main":
                    return True, pdfs
        return False, pdfs

    async def _create_attachment(self, item_key: str, pdf_path: Path) -> str:
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
            "POST", "/users/0/items", json=[payload], headers={"Zotero-Write-Token": secrets.token_hex(16)}
        )
        return self._successful_key(response)

    async def _upload_file(self, attachment_key: str, pdf_path: Path) -> None:
        digest = hashlib.md5(pdf_path.read_bytes(), usedforsecurity=False).hexdigest()
        form = {
            "md5": digest,
            "filename": pdf_path.name,
            "filesize": str(pdf_path.stat().st_size),
            "mtime": str(int(pdf_path.stat().st_mtime * 1000)),
        }
        condition = {"If-None-Match": "*"}
        response = await self._request(
            "POST", f"/users/0/items/{attachment_key}/file", data=form, headers=condition
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
                f"/users/0/items/{attachment_key}/file",
                data={"upload": authorization["uploadKey"]},
                headers=condition,
            )

    async def commit_paper(
        self, collection_key: str, metadata: dict[str, Any], pdf_path: Path | None
    ) -> dict[str, Any]:
        await self._ensure_server()
        matches = await self._candidate_items(metadata)
        if len(matches) > 1:
            raise ZoteroError("Zotero 中有多个匹配题录，需要人工处理")
        created = False
        if matches:
            item = matches[0]
            await self._add_to_collection(item, collection_key)
            item_key = _data(item).get("key") or item.get("key")
        else:
            response = await self._request(
                "POST",
                "/users/0/items",
                json=[item_payload(metadata, collection_key)],
                headers={"Zotero-Write-Token": secrets.token_hex(16)},
            )
            item_key = self._successful_key(response)
            created = True

        existing_main, children = await self._existing_main_pdf(item_key, metadata)
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
                    if incomplete else await self._create_attachment(item_key, pdf_path)
                )
                await self._upload_file(attachment_key, pdf_path)
                attached = True

        final = (await self._request("GET", f"/users/0/items/{item_key}")).json()
        if len(match_items([final], metadata)) != 1:
            raise ZoteroError("Zotero 写入后题录核验失败")
        final_children = await self._children(item_key)
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

    async def export_row(self, item_key: str, fallback_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._ensure_server()
        item = (await self._request("GET", f"/users/0/items/{item_key}")).json()
        metadata = self.metadata_from_item(item)
        if fallback_metadata:
            for key in ("doi", "title", "year", "authors", "journal", "volume", "issue", "pages", "url"):
                if not metadata.get(key) and fallback_metadata.get(key):
                    metadata[key] = fallback_metadata[key]
        children = await self._children(item_key)
        pdf_path = None
        attachment_key = None
        attachment_version = None
        for child in children:
            data = _data(child)
            if data.get("contentType") != "application/pdf":
                continue
            key = data.get("key") or child.get("key")
            path = await self._attachment_path(key)
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

    async def list_collections(self) -> list[dict[str, Any]]:
        await self._ensure_server()
        items = (await self._request("GET", "/users/0/collections")).json()
        result = []
        for item in items:
            data = _data(item)
            result.append(
                {
                    "key": data.get("key") or item.get("key"),
                    "name": data.get("name") or "",
                    "numItems": (item.get("meta") or {}).get("numItems"),
                }
            )
        return result

    async def iter_collection_items(self, name: str):
        key = await self.ensure_collection(name, create=False)
        start = 0
        while True:
            response = await self._request(
                "GET",
                f"/users/0/collections/{key}/items/top",
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

    async def rename_attachment_filename(self, attachment_key: str, filename: str, version: int | None) -> None:
        payload: dict[str, Any] = {"filename": filename, "title": filename}
        if version is not None:
            payload["version"] = version
        await self._request("PATCH", f"/users/0/items/{attachment_key}", json=payload)

