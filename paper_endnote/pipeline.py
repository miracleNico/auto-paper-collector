from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from .browser import BrowserSession
from .clients import (
    CrossrefClient,
    RemoteServiceError,
    UnpaywallClient,
    mcgill_proxy_url,
    mcgill_worldcat_url,
    scholar_search_url,
)
from .config import Settings
from .db import Database
from .downloader import download_pdf, safe_filename
from .endnote import EndNoteAdapter, EndNoteError
from .inputs import normalize_doi, title_similarity
from .pdf_validation import validate_pdf
from .zotero import ZoteroAdapter


class PipelineManager:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.db = database
        self.crossref = CrossrefClient(settings)
        self.unpaywall = UnpaywallClient(settings)
        self.browser = BrowserSession(settings)
        self.browser.set_download_callback(self._browser_downloaded)
        self.endnote = EndNoteAdapter(settings.endnote_exe, settings.generated_dir, settings.backup_dir)
        self.zotero = ZoteroAdapter(database.get_setting("zotero_api_key", ""))
        self._batch_tasks: dict[str, asyncio.Task] = {}
        self._commit_tasks: dict[str, asyncio.Task] = {}
        self._endnote_commit_lock = asyncio.Lock()

    async def close(self) -> None:
        for task in [*self._batch_tasks.values(), *self._commit_tasks.values()]:
            if not task.done():
                task.cancel()
        await self.crossref.close()
        await self.unpaywall.close()
        await self.browser.close()
        await self.zotero.close()

    def start(self, batch_id: str) -> None:
        current = self._batch_tasks.get(batch_id)
        if current and not current.done():
            return
        self.db.update_batch(batch_id, status="running", paused=0, error=None)
        self._batch_tasks[batch_id] = asyncio.create_task(self._run_batch(batch_id))

    def pause(self, batch_id: str) -> None:
        self.db.update_batch(batch_id, status="paused", paused=1)
        self.db.event(batch_id, "批次已暂停")

    def resume(self, batch_id: str) -> None:
        self.start(batch_id)

    async def _run_batch(self, batch_id: str) -> None:
        try:
            while True:
                batch = self.db.get_batch(batch_id)
                if not batch or batch["paused"]:
                    return
                actionable = [
                    paper for paper in batch["papers"]
                    if paper["status"] in {"queued", "matching", "ready", "looking_for_pdf"}
                ]
                if not actionable:
                    self.db.update_batch(batch_id, status="completed")
                    self.db.event(batch_id, "网络与题录准备阶段已完成")
                    return
                paper = actionable[0]
                try:
                    await self._process_paper(batch_id, paper)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.db.update_paper(
                        paper["id"], status="failed", needs_action="retry", error=str(exc)
                    )
                    self.db.event(batch_id, f"处理失败：{exc}", level="error", paper_id=paper["id"])
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.db.update_batch(batch_id, status="failed", error=str(exc))
            self.db.event(batch_id, f"批次失败：{exc}", level="error")

    async def _process_paper(self, batch_id: str, paper: dict[str, Any]) -> None:
        if paper["metadata_status"] != "verified":
            await self._resolve_metadata(batch_id, paper)
            paper = self.db.get_paper(paper["id"])
            if not paper or paper["metadata_status"] != "verified":
                return
        if paper["pdf_status"] in {"verified", "accepted"}:
            self.db.update_paper(paper["id"], status="endnote_pending", endnote_status="pending", needs_action="commit_endnote")
            return
        await self._find_pdf(batch_id, paper)

    async def _resolve_metadata(self, batch_id: str, paper: dict[str, Any]) -> None:
        self.db.update_paper(paper["id"], status="matching", metadata_status="matching", error=None)
        try:
            if paper.get("input_doi"):
                work = await self.crossref.by_doi(paper["input_doi"])
                supplied = paper.get("input_title")
                if supplied and title_similarity(supplied, work.get("title")) < 0.65:
                    work["score"] = title_similarity(supplied, work.get("title"))
                    self.db.replace_candidates(paper["id"], [work])
                    self.db.update_paper(
                        paper["id"], status="needs_match", metadata_status="needs_review",
                        needs_action="confirm_metadata", error="输入题名与 DOI 元数据不一致",
                    )
                    return
                self._accept_metadata(paper["id"], work)
                self.db.event(batch_id, "已通过 DOI 获取题录", paper_id=paper["id"])
                return

            title = paper.get("input_title") or paper["input_text"]
            candidates = await self.crossref.search(title)
            for candidate in candidates:
                candidate["similarity"] = title_similarity(title, candidate.get("title"))
            self.db.replace_candidates(paper["id"], candidates)
            strong = [item for item in candidates if item["similarity"] >= 0.93]
            if len(strong) == 1:
                candidate = strong[0]
                input_year = paper.get("input_year")
                if input_year and candidate.get("year") and int(input_year) != int(candidate["year"]):
                    strong = []
            if len(strong) == 1:
                self._accept_metadata(paper["id"], strong[0])
                self.db.event(batch_id, "已通过题名唯一匹配题录", paper_id=paper["id"])
            else:
                self.db.update_paper(
                    paper["id"], status="needs_match", metadata_status="needs_review",
                    needs_action="confirm_metadata", error="需要确认 Crossref 匹配结果",
                )
        except RemoteServiceError as exc:
            self.db.update_paper(
                paper["id"], status="needs_match", metadata_status="unavailable",
                needs_action="retry", error=str(exc),
            )

    def _accept_metadata(self, paper_id: str, work: dict[str, Any]) -> None:
        self.db.update_paper(
            paper_id,
            doi=normalize_doi(work.get("doi")),
            title=work.get("title") or "",
            year=work.get("year"),
            authors_json=work.get("authors") or [],
            journal=work.get("journal") or "",
            metadata_json=work,
            metadata_status="verified",
            status="ready",
            needs_action=None,
            error=None,
        )

    async def _find_pdf(self, batch_id: str, paper: dict[str, Any]) -> None:
        self.db.update_paper(paper["id"], status="looking_for_pdf", pdf_status="searching", error=None)
        doi = paper.get("doi")
        location = await self.unpaywall.best_location(doi) if doi else None
        if location and location.version == "publishedVersion" and location.pdf_url:
            filename = safe_filename(f"{doi or paper['id']}.pdf")
            destination = self.settings.download_dir / paper["id"] / filename
            try:
                await download_pdf(location.pdf_url, destination, self.settings)
                result = validate_pdf(destination, expected_doi=doi, expected_title=paper.get("title"))
                if result.identity == "verified" and result.role == "main":
                    self.db.update_paper(
                        paper["id"], pdf_status="verified", status="endnote_pending", version="published",
                        source_url=location.url, pdf_path=str(destination), pdf_sha256=result.sha256,
                        endnote_status="pending", needs_action="commit_endnote", error=None,
                    )
                    self.db.event(batch_id, "已取得并验证开放获取正式版 PDF", paper_id=paper["id"])
                    return
                self.db.update_paper(
                    paper["id"], pdf_status="needs_review", status="needs_pdf_review", version="published",
                    source_url=location.url, pdf_path=str(destination), pdf_sha256=result.sha256,
                    needs_action="confirm_pdf", error=result.reason,
                )
                return
            except Exception as exc:
                self.db.event(batch_id, f"开放全文自动获取失败：{exc}", level="warning", paper_id=paper["id"])

        if location:
            version_map = {"acceptedVersion": "accepted", "submittedVersion": "preprint", "publishedVersion": "published"}
            self.db.update_paper(
                paper["id"], pdf_status="not_downloaded", status="needs_pdf",
                version=version_map.get(location.version, "unknown"), source_url=location.url,
                needs_action="manual_pdf", error="找到候选全文，请在浏览器中确认或提供 PDF",
            )
            return

        target = paper.get("metadata", {}).get("url") or (f"https://doi.org/{doi}" if doi else scholar_search_url(paper.get("title") or paper["input_text"]))
        self.db.update_paper(
            paper["id"], pdf_status="not_found", status="needs_pdf",
            source_url=mcgill_proxy_url(target) if doi else target,
            needs_action="manual_pdf", error="未找到可自动获取的开放正式版；请通过 McGill 或 Scholar 处理",
            endnote_status="pending",
        )

    def confirm_metadata(self, paper_id: str, metadata: dict[str, Any]) -> None:
        paper = self.db.get_paper(paper_id)
        if not paper:
            raise KeyError(paper_id)
        self._accept_metadata(paper_id, metadata)
        self.db.event(paper["batch_id"], "已确认题录", paper_id=paper_id)
        self.start(paper["batch_id"])

    async def accept_local_pdf(self, paper_id: str, source: Path, *, accepted_by_user: bool = False) -> dict[str, Any]:
        paper = self.db.get_paper(paper_id)
        if not paper:
            raise KeyError(paper_id)
        destination = self.settings.download_dir / paper_id / safe_filename(source.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        result = validate_pdf(destination, expected_doi=paper.get("doi"), expected_title=paper.get("title"))
        if result.identity == "rejected":
            destination.unlink(missing_ok=True)
            raise ValueError(result.reason)
        if result.identity == "verified" and result.role == "main":
            self.db.update_paper(
                paper_id, pdf_status="verified", status="endnote_pending", pdf_path=str(destination),
                pdf_sha256=result.sha256, endnote_status="pending", needs_action="commit_endnote", error=None,
            )
        elif not accepted_by_user:
            self.db.update_paper(
                paper_id, pdf_status="needs_review", status="needs_pdf_review", pdf_path=str(destination),
                pdf_sha256=result.sha256, needs_action="confirm_pdf", error=result.reason,
            )
        else:
            self.db.update_paper(
                paper_id, pdf_status="accepted" if accepted_by_user else "verified", status="endnote_pending",
                pdf_path=str(destination), pdf_sha256=result.sha256, endnote_status="pending",
                needs_action="commit_endnote", error=None,
            )
        self.db.event(paper["batch_id"], f"已接收本地 PDF：{result.reason}", paper_id=paper_id)
        return result.as_dict()

    async def acquire_institution_pdf(self, paper_id: str) -> dict[str, Any]:
        paper = self.db.get_paper(paper_id)
        if not paper:
            raise KeyError(paper_id)
        if paper.get("metadata_status") != "verified":
            raise ValueError("请先确认题录，再通过机构获取 PDF")
        target = paper.get("source_url")
        if not target:
            doi = paper.get("doi")
            target = mcgill_proxy_url(f"https://doi.org/{doi}") if doi else scholar_search_url(paper.get("title") or paper["input_text"])
        self.db.event(paper["batch_id"], "正在使用已登录 Chrome 会话尝试单篇机构获取", paper_id=paper_id)
        try:
            result = await self.browser.acquire_for_paper(paper_id, target)
            updated = self.db.get_paper(paper_id)
            if not updated or updated.get("pdf_status") != "verified":
                raise ValueError(updated.get("error") if updated else "PDF 下载后的身份核验未通过")
            self.db.update_paper(
                paper_id, version="published", source_url=result.get("source_url") or target, error=None
            )
            self.db.event(paper["batch_id"], "机构正式版 PDF 已下载并通过身份校验", paper_id=paper_id)
            return result
        except Exception as exc:
            self.db.update_paper(
                paper_id, status="needs_pdf", pdf_status="not_found", needs_action="manual_pdf", error=str(exc)
            )
            self.db.event(paper["batch_id"], f"机构自动获取未完成：{exc}", level="warning", paper_id=paper_id)
            raise

    async def _browser_downloaded(self, paper_id: str, path: Path) -> None:
        try:
            await self.accept_local_pdf(paper_id, path)
        except Exception as exc:
            paper = self.db.get_paper(paper_id)
            if paper:
                self.db.event(paper["batch_id"], f"浏览器下载文件未通过校验：{exc}", level="error", paper_id=paper_id)

    async def open_paper_url(
        self, paper_id: str, *, scholar: bool = False, resolver: bool = False
    ) -> None:
        paper = self.db.get_paper(paper_id)
        if not paper:
            raise KeyError(paper_id)
        if resolver:
            doi = paper.get("doi")
            if not doi:
                raise ValueError("没有 DOI，无法打开 McGill 馆藏解析器")
            url = mcgill_worldcat_url(doi)
        elif scholar:
            url = scholar_search_url(paper.get("doi") or paper.get("title") or paper["input_text"])
        else:
            url = paper.get("source_url") or scholar_search_url(paper.get("doi") or paper.get("title") or paper["input_text"])
        await self.browser.open_for_paper(paper_id, url)

    def commit(self, batch_id: str) -> None:
        current = self._commit_tasks.get(batch_id)
        if current and not current.done():
            return
        self._commit_tasks[batch_id] = asyncio.create_task(self._commit_batch(batch_id))

    async def _commit_batch(self, batch_id: str) -> None:
        async with self._endnote_commit_lock:
            batch = self.db.get_batch(batch_id)
            if not batch:
                return
            if batch.get("reference_manager") == "zotero":
                await self._commit_zotero_batch(batch)
                return
            library = self.endnote.validate_library_path(batch["target_library"], batch["library_mode"])
            try:
                if batch["library_mode"] == "existing" and not batch.get("backup_path"):
                    backup = await asyncio.to_thread(self.endnote.create_filesystem_backup, library, batch_id)
                    self.db.update_batch(batch_id, backup_path=str(backup))
                    self.db.event(batch_id, f"已创建目标库备份：{backup}")
                if batch["library_mode"] == "new":
                    await asyncio.to_thread(self.endnote.create_library, library)
                    self.db.update_batch(batch_id, library_mode="existing")
                else:
                    await asyncio.to_thread(self.endnote.open_library, library)
            except Exception as exc:
                self.db.update_batch(batch_id, status="failed", error=str(exc))
                self.db.event(batch_id, f"EndNote 准备失败：{exc}", level="error")
                return

            for paper in self.db.get_batch(batch_id)["papers"]:
                if paper["metadata_status"] != "verified" or paper["endnote_status"] == "verified":
                    continue
                operation_id = self.db.start_operation(paper["id"], "endnote_commit", {"library": str(library)})
                self.db.update_paper(paper["id"], endnote_status="pending_commit")
                metadata = dict(paper.get("metadata") or {})
                metadata.update({
                    "doi": paper.get("doi"), "title": paper.get("title"), "year": paper.get("year"),
                    "authors": paper.get("authors") or [], "journal": paper.get("journal") or "",
                })
                pdf_path = Path(paper["pdf_path"]) if paper.get("pdf_path") and paper["pdf_status"] in {"verified", "accepted"} else None
                try:
                    result = await asyncio.to_thread(self.endnote.commit_paper, library, paper["id"], metadata, pdf_path)
                    has_full_text = bool(pdf_path or result.get("existing_full_text"))
                    final_status = "complete" if has_full_text else "needs_pdf"
                    needs_action = None if has_full_text else "manual_pdf"
                    self.db.update_paper(
                        paper["id"], endnote_status="verified", status=final_status,
                        record_number=result.get("record_number"), needs_action=needs_action, error=None,
                    )
                    self.db.finish_operation(operation_id, "verified", result)
                    self.db.event(batch_id, "EndNote 写入及对账完成", paper_id=paper["id"])
                except Exception as exc:
                    self.db.finish_operation(operation_id, "uncertain", {"error": str(exc)})
                    self.db.update_paper(
                        paper["id"], endnote_status="uncertain", status="failed",
                        needs_action="reconcile_endnote", error=str(exc),
                    )
                    self.db.event(batch_id, f"EndNote 提交状态不确定：{exc}", level="error", paper_id=paper["id"])
            self.db.update_batch(batch_id, status="completed")
            self.db.event(batch_id, "EndNote 提交阶段结束")

    async def _commit_zotero_batch(self, batch: dict[str, Any]) -> None:
        batch_id = batch["id"]
        collection_name = batch["target_library"].strip()
        try:
            collection_key = await self.zotero.ensure_collection(
                collection_name, create=batch["library_mode"] == "new"
            )
            if batch["library_mode"] == "new":
                self.db.update_batch(batch_id, library_mode="existing")
        except Exception as exc:
            self.db.update_batch(batch_id, status="failed", error=str(exc))
            self.db.event(batch_id, f"Zotero 准备失败：{exc}", level="error")
            return

        for paper in self.db.get_batch(batch_id)["papers"]:
            if paper["metadata_status"] != "verified" or paper["endnote_status"] == "verified":
                continue
            operation_id = self.db.start_operation(
                paper["id"], "zotero_commit", {"collection": collection_name, "collection_key": collection_key}
            )
            self.db.update_paper(paper["id"], endnote_status="pending_commit")
            metadata = dict(paper.get("metadata") or {})
            metadata.update({
                "doi": paper.get("doi"), "title": paper.get("title"), "year": paper.get("year"),
                "authors": paper.get("authors") or [], "journal": paper.get("journal") or "",
            })
            pdf_path = (
                Path(paper["pdf_path"])
                if paper.get("pdf_path") and paper["pdf_status"] in {"verified", "accepted"}
                else None
            )
            try:
                result = await self.zotero.commit_paper(collection_key, metadata, pdf_path)
                has_full_text = bool(pdf_path or result.get("existing_full_text"))
                self.db.update_paper(
                    paper["id"],
                    endnote_status="verified",
                    status="complete" if has_full_text else "needs_pdf",
                    record_number=result.get("record_number"),
                    needs_action=None if has_full_text else "manual_pdf",
                    error=None,
                )
                self.db.finish_operation(operation_id, "verified", result)
                self.db.event(batch_id, "Zotero 写入及对账完成", paper_id=paper["id"])
            except Exception as exc:
                self.db.finish_operation(operation_id, "uncertain", {"error": str(exc)})
                self.db.update_paper(
                    paper["id"], endnote_status="uncertain", status="failed",
                    needs_action="reconcile_endnote", error=str(exc),
                )
                self.db.event(batch_id, f"Zotero 提交状态不确定：{exc}", level="error", paper_id=paper["id"])
        self.db.update_batch(batch_id, status="completed")
        self.db.event(batch_id, "Zotero 提交阶段结束")
