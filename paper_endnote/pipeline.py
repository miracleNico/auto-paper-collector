from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .browser import BrowserSession, LoginTimeoutError, PublisherBlockedError
from .clients import (
    CrossrefClient,
    RemoteServiceError,
    UnpaywallClient,
    scholar_search_url,
)
from .config import Settings
from .db import BatchDeletingError, Database
from .downloader import download_pdf, safe_filename
from .inputs import normalize_doi, title_similarity
from .library_files import downloads_library_dir, export_zotero_endnote_package
from .pdf_validation import validate_pdf
from .user_config import institution_openurl, institution_proxy_url
from .zotero import ZoteroAdapter


class PipelineManager:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.db = database
        self.crossref = CrossrefClient(settings)
        self.unpaywall = UnpaywallClient(settings)
        self.browser = BrowserSession(settings)
        self.browser.set_download_callback(self._browser_downloaded)
        self.zotero = ZoteroAdapter(database.get_setting("zotero_api_key", ""))
        self._batch_tasks: dict[str, asyncio.Task] = {}
        self._commit_tasks: dict[str, asyncio.Task] = {}
        self._cancellable_work: dict[str, set[asyncio.Task]] = {}
        self._drain_work: dict[str, set[asyncio.Task]] = {}
        self._deleting_batches: set[str] = set()
        self._commit_lock = asyncio.Lock()
        self.institution_gap_seconds = 6.0
        self.institution_discovery_timeout_seconds = 180.0
        self._institution_session_ready = False
        self._institution_login_failed = False
        # Browser downloads normally update the paper immediately.  During a
        # two-source race they must remain staged until the winner is chosen.
        self._staged_browser_papers: set[str] = set()
        self._staged_browser_downloads: dict[str, set[Path]] = {}

    async def close(self) -> None:
        tasks = [
            *self._batch_tasks.values(),
            *self._commit_tasks.values(),
            *(task for tasks in self._cancellable_work.values() for task in tasks),
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.crossref.close()
        await self.unpaywall.close()
        await self.browser.close()
        await self.zotero.close()

    def start(self, batch_id: str) -> None:
        self.db.assert_batch_writable(batch_id)
        current = self._batch_tasks.get(batch_id)
        if current and not current.done():
            return
        self.db.update_batch(batch_id, status="running", paused=0, error=None)
        self._batch_tasks[batch_id] = asyncio.create_task(self._run_batch(batch_id))

    def pause(self, batch_id: str) -> None:
        self.db.assert_batch_writable(batch_id)
        self.db.update_batch(batch_id, status="paused", paused=1)
        self.db.event(batch_id, "批次已暂停")

    def resume(self, batch_id: str) -> None:
        self.start(batch_id)

    def is_batch_running(self, batch_id: str) -> bool:
        tasks = [self._batch_tasks.get(batch_id), self._commit_tasks.get(batch_id)]
        return any(task is not None and not task.done() for task in tasks) or any(
            not task.done()
            for task in (
                self._cancellable_work.get(batch_id, set())
                | self._drain_work.get(batch_id, set())
            )
        )

    @asynccontextmanager
    async def track_batch_work(
        self, batch_id: str, *, cancellable: bool
    ) -> AsyncIterator[None]:
        self.db.assert_batch_writable(batch_id)
        if batch_id in self._deleting_batches:
            raise BatchDeletingError("批次正在删除，不能再修改")
        task = asyncio.current_task()
        if task is None:
            yield
            return
        work = self._cancellable_work if cancellable else self._drain_work
        work.setdefault(batch_id, set()).add(task)
        try:
            yield
        finally:
            tasks = work.get(batch_id)
            if tasks is not None:
                tasks.discard(task)
                if not tasks:
                    work.pop(batch_id, None)

    async def prepare_batch_deletion(self, batch_id: str, paper_ids: list[str]) -> None:
        """Stop acquisition, drain file/external writes, and tombstone browser callbacks."""
        self._deleting_batches.add(batch_id)
        await self.browser.stop_papers(paper_ids)

        current = asyncio.current_task()
        cancellable = set(self._cancellable_work.get(batch_id, set()))
        batch_task = self._batch_tasks.get(batch_id)
        if batch_task is not None:
            cancellable.add(batch_task)
        cancellable.discard(current)
        for task in cancellable:
            if not task.done():
                task.cancel()
        if cancellable:
            await asyncio.gather(*cancellable, return_exceptions=True)

        commit_task = self._commit_tasks.get(batch_id)
        if commit_task is not None and not commit_task.done():
            await asyncio.shield(commit_task)
        draining = {
            task for task in self._drain_work.get(batch_id, set())
            if task is not current and not task.done()
        }
        if draining:
            await asyncio.gather(*(asyncio.shield(task) for task in draining))
        await self.browser.stop_papers(paper_ids)

    async def _run_batch(self, batch_id: str) -> None:
        self._institution_session_ready = False
        self._institution_login_failed = False
        try:
            while True:
                batch = self.db.get_batch(batch_id)
                if not batch or batch["paused"]:
                    return
                actionable = [
                    paper for paper in batch["papers"]
                    if paper["status"] in {
                        "queued", "matching", "ready", "looking_for_pdf", "institution_pending",
                    }
                ]
                if not actionable:
                    self.db.update_batch(batch_id, status="completed")
                    self.db.event(batch_id, "网络与题录准备阶段已完成")
                    if self.settings.auto_commit:
                        self.commit(batch_id)
                        await asyncio.shield(self._commit_tasks[batch_id])
                    return
                paper = actionable[0]
                try:
                    if paper["status"] == "institution_pending":
                        await self._process_institution_paper(batch_id, paper)
                        if self.institution_gap_seconds:
                            await asyncio.sleep(self.institution_gap_seconds)
                    else:
                        await self._process_paper(batch_id, paper)
                except asyncio.CancelledError:
                    raise
                except LoginTimeoutError as exc:
                    self._institution_login_failed = True
                    self._downgrade_remaining_institution(batch_id, str(exc))
                    self.db.event(batch_id, f"等待登录超时：{exc}", level="warning", paper_id=paper["id"])
                except Exception as exc:
                    self.db.update_paper(
                        paper["id"], status="failed", needs_action="retry", error=str(exc)
                    )
                    self.db.event(batch_id, f"处理失败：{exc}", level="error", paper_id=paper["id"])
        except asyncio.CancelledError:
            return
        except BatchDeletingError:
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

    def _validate_pdf(self, path: Path, *, expected_doi: str | None, expected_title: str | None):
        return validate_pdf(
            path,
            expected_doi=expected_doi,
            expected_title=expected_title,
            **self.settings.pdf_ocr_kwargs(),
        )

    def _institution_entry(self, *, doi: str | None, fallback: str) -> str:
        if doi:
            return institution_proxy_url(self.settings.institution, f"https://doi.org/{doi}")
        return fallback

    async def _find_pdf(self, batch_id: str, paper: dict[str, Any]) -> None:
        self.db.update_paper(paper["id"], status="looking_for_pdf", pdf_status="searching", error=None)
        doi = paper.get("doi")
        sources = self.settings.acquisition_sources
        if (
            "open_access" in sources
            and "institution" in sources
            and self.settings.auto_institution
        ):
            await self._race_pdf_sources(batch_id, paper)
            return
        if "open_access" in sources:
            location = await self.unpaywall.best_location(doi) if doi else None
            if location and location.version == "publishedVersion" and location.pdf_url:
                filename = safe_filename(f"{doi or paper['id']}.pdf")
                destination = self.settings.download_dir / paper["id"] / filename
                try:
                    await download_pdf(location.pdf_url, destination, self.settings)
                    result = self._validate_pdf(destination, expected_doi=doi, expected_title=paper.get("title"))
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
            elif location and "institution" not in sources:
                version_map = {"acceptedVersion": "accepted", "submittedVersion": "preprint", "publishedVersion": "published"}
                self.db.update_paper(
                    paper["id"], pdf_status="not_downloaded", status="needs_pdf",
                    version=version_map.get(location.version, "unknown"), source_url=location.url,
                    needs_action="manual_pdf", error="找到候选全文，请在浏览器中确认或提供 PDF",
                )
                return

        fallback = paper.get("metadata", {}).get("url") or (
            f"https://doi.org/{doi}" if doi else scholar_search_url(paper.get("title") or paper["input_text"])
        )
        if "institution" in sources:
            source_url = self._institution_entry(doi=doi, fallback=fallback)
            if self.settings.auto_institution:
                if self._institution_login_failed:
                    self.db.update_paper(
                        paper["id"], pdf_status="not_found", status="needs_pdf",
                        source_url=source_url,
                        needs_action="manual_pdf",
                        error="等待登录超时，本批剩余条目已转入人工队列",
                        endnote_status="pending",
                    )
                    return
                self.db.update_paper(
                    paper["id"],
                    pdf_status="not_found",
                    status="institution_pending",
                    source_url=source_url,
                    needs_action=None,
                    error=None,
                    endnote_status="pending",
                )
                self.db.event(
                    batch_id,
                    f"开放获取未命中，转入 {self.settings.institution.name} 自动获取",
                    paper_id=paper["id"],
                )
                return
            self.db.update_paper(
                paper["id"], pdf_status="not_found", status="needs_pdf",
                source_url=source_url,
                needs_action="manual_pdf",
                error=f"未找到可自动获取的开放正式版；请通过 {self.settings.institution.name} 处理",
                endnote_status="pending",
            )
            return
        self.db.update_paper(
            paper["id"], pdf_status="not_found", status="needs_pdf",
            source_url=fallback,
            needs_action="manual_pdf", error="未找到可自动获取的开放正式版",
            endnote_status="pending",
        )

    async def _try_open_access_pdf(self, paper: dict[str, Any]) -> dict[str, Any]:
        """Download and validate OA into staging without changing paper state."""
        doi = paper.get("doi")
        if not doi:
            return {"success": False, "source": "open_access", "error": "没有 DOI"}
        destination = self.settings.download_dir / paper["id"] / "open-access-main.pdf"
        try:
            location = await self.unpaywall.best_location(doi)
            if not location:
                return {"success": False, "source": "open_access", "error": "未找到开放全文"}
            if location.version != "publishedVersion" or not location.pdf_url:
                return {
                    "success": False,
                    "source": "open_access",
                    "source_url": location.url,
                    "version": location.version,
                    "error": "开放候选不是可自动采用的正式发表版 PDF",
                }
            await download_pdf(location.pdf_url, destination, self.settings)
            result = self._validate_pdf(
                destination, expected_doi=doi, expected_title=paper.get("title")
            )
            if result.identity == "verified" and result.role == "main":
                return {
                    "success": True,
                    "source": "open_access",
                    "source_url": location.url,
                    "version": "published",
                    "path": destination,
                    "sha256": result.sha256,
                }
            if result.valid_pdf and result.identity == "needs_review":
                return {
                    "success": False,
                    "review": True,
                    "source": "open_access",
                    "source_url": location.url,
                    "version": "published",
                    "path": destination,
                    "sha256": result.sha256,
                    "role": result.role,
                    "title_similarity": result.title_similarity,
                    "error": result.reason,
                }
            destination.unlink(missing_ok=True)
            return {
                "success": False,
                "source": "open_access",
                "source_url": location.url,
                "error": result.reason,
            }
        except asyncio.CancelledError:
            destination.unlink(missing_ok=True)
            destination.with_suffix(destination.suffix + ".part").unlink(missing_ok=True)
            raise
        except Exception as exc:
            destination.unlink(missing_ok=True)
            return {"success": False, "source": "open_access", "error": str(exc)}

    async def _try_institution_pdf(
        self, batch_id: str, paper: dict[str, Any]
    ) -> dict[str, Any]:
        """Acquire one institutional PDF into staging without committing it."""
        paper_id = paper["id"]
        doi = paper.get("doi")
        fallback = paper.get("metadata", {}).get("url") or (
            f"https://doi.org/{doi}"
            if doi
            else scholar_search_url(paper.get("title") or paper["input_text"])
        )
        target = self._institution_entry(doi=doi, fallback=fallback)
        destination = self.settings.download_dir / paper_id / "institution-main.pdf"
        if self._institution_login_failed:
            return {
                "success": False,
                "source": "institution",
                "source_url": target,
                "error": "等待登录超时，本批剩余条目已转入人工队列",
            }
        download_started = False
        try:
            if not self._institution_session_ready:
                await self.browser.ensure_logged_in(target)
                self._institution_session_ready = True
                self.db.event(
                    batch_id,
                    f"{self.settings.institution.name} 会话已就绪",
                    paper_id=paper_id,
                )

            async with asyncio.timeout(
                self.institution_discovery_timeout_seconds
            ) as discovery_timeout:
                def mark_download_started() -> None:
                    nonlocal download_started
                    if download_started:
                        return
                    download_started = True
                    discovery_timeout.reschedule(None)

                browser_result = await self.browser.acquire_for_paper(
                    paper_id, target, on_download_started=mark_download_started
                )
            path = Path(browser_result.get("path") or destination)
            result = self._validate_pdf(
                path, expected_doi=doi, expected_title=paper.get("title")
            )
            if result.identity == "verified" and result.role == "main":
                return {
                    "success": True,
                    "source": "institution",
                    "source_url": browser_result.get("source_url") or target,
                    "version": "published",
                    "path": path,
                    "sha256": result.sha256,
                }
            if result.valid_pdf and result.identity == "needs_review":
                return {
                    "success": False,
                    "review": True,
                    "source": "institution",
                    "source_url": browser_result.get("source_url") or target,
                    "version": "published",
                    "path": path,
                    "sha256": result.sha256,
                    "role": result.role,
                    "title_similarity": result.title_similarity,
                    "error": result.reason,
                }
            path.unlink(missing_ok=True)
            return {
                "success": False,
                "source": "institution",
                "source_url": browser_result.get("source_url") or target,
                "error": result.reason,
            }
        except asyncio.CancelledError:
            destination.unlink(missing_ok=True)
            destination.with_suffix(destination.suffix + ".part").unlink(missing_ok=True)
            raise
        except LoginTimeoutError as exc:
            destination.unlink(missing_ok=True)
            destination.with_suffix(destination.suffix + ".part").unlink(missing_ok=True)
            self._institution_login_failed = True
            return {
                "success": False,
                "source": "institution",
                "source_url": target,
                "error": str(exc),
            }
        except TimeoutError as exc:
            destination.unlink(missing_ok=True)
            destination.with_suffix(destination.suffix + ".part").unlink(missing_ok=True)
            if download_started:
                message = str(exc).strip() or "下载或 PDF 校验阶段超时"
            else:
                seconds = f"{self.institution_discovery_timeout_seconds:g}"
                message = f"机构 PDF 入口发现超过单篇 {seconds} 秒上限"
            return {
                "success": False,
                "source": "institution",
                "source_url": target,
                "error": message,
            }
        except Exception as exc:
            destination.unlink(missing_ok=True)
            destination.with_suffix(destination.suffix + ".part").unlink(missing_ok=True)
            return {
                "success": False,
                "source": "institution",
                "source_url": target,
                "error": str(exc),
            }

    def _cleanup_race_files(self, paper_id: str, *, keep: Path | None = None) -> None:
        """Remove only known per-paper staging files, preserving the selected candidate."""
        paper_dir = (self.settings.download_dir / paper_id).resolve()
        paths = {
            paper_dir / "open-access-main.pdf",
            paper_dir / "open-access-main.pdf.part",
            paper_dir / "institution-main.pdf",
            paper_dir / "institution-main.pdf.part",
            *self._staged_browser_downloads.get(paper_id, set()),
        }
        keep_resolved = keep.resolve() if keep else None
        for path in paths:
            resolved = path.resolve()
            try:
                resolved.relative_to(paper_dir)
            except ValueError:
                continue
            if keep_resolved is None or resolved != keep_resolved:
                resolved.unlink(missing_ok=True)

    def _browser_stage_outcomes(
        self, paper: dict[str, Any], known: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate browser downloads captured while a two-source race was active."""
        paper_id = paper["id"]
        known_paths = {
            Path(item["path"]).resolve()
            for item in known
            if item.get("path")
        }
        outcomes: list[dict[str, Any]] = []
        for path in self._staged_browser_downloads.get(paper_id, set()):
            resolved = path.resolve()
            if resolved in known_paths or not resolved.is_file():
                continue
            result = self._validate_pdf(
                resolved,
                expected_doi=paper.get("doi"),
                expected_title=paper.get("title"),
            )
            base = {
                "source": "institution",
                "source_url": paper.get("source_url") or self._institution_entry(
                    doi=paper.get("doi"),
                    fallback=paper.get("metadata", {}).get("url") or "",
                ),
                "version": "published",
                "path": resolved,
                "sha256": result.sha256,
                "role": result.role,
                "title_similarity": result.title_similarity,
                "error": result.reason,
            }
            if result.identity == "verified" and result.role == "main":
                outcomes.append({**base, "success": True})
            elif result.valid_pdf and result.identity == "needs_review":
                outcomes.append({**base, "success": False, "review": True})
            else:
                resolved.unlink(missing_ok=True)
                outcomes.append({**base, "success": False})
        return outcomes

    async def _race_pdf_sources(self, batch_id: str, paper: dict[str, Any]) -> None:
        """Race OA and institution for one paper; commit exactly one winner."""
        paper_id = paper["id"]
        self._staged_browser_papers.add(paper_id)
        self._staged_browser_downloads[paper_id] = set()
        oa_task = asyncio.create_task(self._try_open_access_pdf(paper))
        institution_task = asyncio.create_task(
            self._try_institution_pdf(batch_id, paper)
        )
        pending: set[asyncio.Task] = {oa_task, institution_task}
        outcomes: list[dict[str, Any]] = []
        keep_path: Path | None = None
        try:
            winner: dict[str, Any] | None = None
            while pending and winner is None:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        outcome = task.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception as exc:
                        outcome = {"success": False, "error": str(exc)}
                    outcomes.append(outcome)
                    if outcome.get("success") and winner is None:
                        winner = outcome
                if winner is not None:
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    pending.clear()

            wait_for_downloads = getattr(self.browser, "wait_for_downloads", None)
            if wait_for_downloads is not None:
                await wait_for_downloads(paper_id)
            staged_outcomes = self._browser_stage_outcomes(paper, outcomes)
            outcomes.extend(staged_outcomes)
            if winner is None:
                winner = next(
                    (item for item in staged_outcomes if item.get("success")), None
                )

            if winner is not None:
                winner_path = Path(winner["path"])
                self.db.update_paper(
                    paper_id,
                    pdf_status="verified",
                    status="endnote_pending",
                    version=winner.get("version") or "published",
                    source_url=winner.get("source_url"),
                    pdf_path=str(winner_path),
                    pdf_sha256=winner.get("sha256"),
                    endnote_status="pending",
                    needs_action="commit_endnote",
                    error=None,
                )
                keep_path = winner_path
                source_name = (
                    "开放获取" if winner.get("source") == "open_access"
                    else self.settings.institution.name
                )
                self.db.event(
                    batch_id,
                    f"{source_name} 路径率先取得并验证正式版 PDF",
                    paper_id=paper_id,
                )
            else:
                review_candidates = [item for item in outcomes if item.get("review")]
                if review_candidates:
                    review = max(
                        review_candidates,
                        key=lambda item: (
                            item.get("role") == "main",
                            float(item.get("title_similarity") or 0.0),
                        ),
                    )
                    review_path = Path(review["path"])
                    self.db.update_paper(
                        paper_id,
                        pdf_status="needs_review",
                        status="needs_pdf_review",
                        version=review.get("version") or "published",
                        source_url=review.get("source_url"),
                        pdf_path=str(review_path),
                        pdf_sha256=review.get("sha256"),
                        needs_action="confirm_pdf",
                        error=review.get("error") or "PDF 需要人工确认",
                        endnote_status="pending",
                    )
                    keep_path = review_path
                    self.db.event(
                        batch_id,
                        "两路均无自动验证结果；已保留最佳 PDF 候选等待确认",
                        level="warning",
                        paper_id=paper_id,
                    )
                else:
                    candidate = next(
                        (item for item in outcomes if item.get("source_url")), {}
                    )
                    errors = [
                        str(item.get("error"))
                        for item in outcomes
                        if item.get("error")
                    ]
                    self.db.update_paper(
                        paper_id,
                        pdf_status="not_found",
                        status="needs_pdf",
                        version=None,
                        source_url=candidate.get("source_url"),
                        pdf_path=None,
                        pdf_sha256=None,
                        needs_action="manual_pdf",
                        error="；".join(errors) or "两条自动获取路径均未取得可验证 PDF",
                        endnote_status="pending",
                    )
                    self.db.event(
                        batch_id,
                        "开放获取与机构路径均未取得可验证 PDF，已转入人工队列",
                        level="warning",
                        paper_id=paper_id,
                    )
        except asyncio.CancelledError:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
        finally:
            self._cleanup_race_files(paper_id, keep=keep_path)
            self._staged_browser_downloads.pop(paper_id, None)
            self._staged_browser_papers.discard(paper_id)
        if self.institution_gap_seconds:
            await asyncio.sleep(self.institution_gap_seconds)

    def confirm_metadata(self, paper_id: str, metadata: dict[str, Any]) -> None:
        self.db.assert_paper_writable(paper_id)
        paper = self.db.get_paper(paper_id)
        if not paper:
            raise KeyError(paper_id)
        self._accept_metadata(paper_id, metadata)
        self.db.event(paper["batch_id"], "已确认题录", paper_id=paper_id)
        self.start(paper["batch_id"])

    async def accept_local_pdf(self, paper_id: str, source: Path, *, accepted_by_user: bool = False) -> dict[str, Any]:
        self.db.assert_paper_writable(paper_id)
        paper = self.db.get_paper(paper_id)
        if not paper:
            raise KeyError(paper_id)
        destination = self.settings.download_dir / paper_id / safe_filename(source.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        result = self._validate_pdf(destination, expected_doi=paper.get("doi"), expected_title=paper.get("title"))
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

    def _downgrade_remaining_institution(self, batch_id: str, error: str) -> None:
        batch = self.db.get_batch(batch_id)
        if not batch:
            return
        message = error or "等待登录超时"
        for paper in batch["papers"]:
            if paper["status"] != "institution_pending":
                continue
            self.db.update_paper(
                paper["id"],
                status="needs_pdf",
                pdf_status="not_found",
                needs_action="manual_pdf",
                error=message,
            )

    async def _process_institution_paper(self, batch_id: str, paper: dict[str, Any]) -> None:
        if not self._institution_session_ready:
            entry = paper.get("source_url") or self._institution_entry(
                doi=paper.get("doi"),
                fallback=scholar_search_url(paper.get("title") or paper["input_text"]),
            )
            await self.browser.ensure_logged_in(entry)
            self._institution_session_ready = True
            self.db.event(batch_id, f"{self.settings.institution.name} 会话已就绪，开始逐篇获取")
        try:
            download_started = False
            async with asyncio.timeout(self.institution_discovery_timeout_seconds) as discovery_timeout:
                def mark_download_started() -> None:
                    nonlocal download_started
                    if download_started:
                        return
                    download_started = True
                    discovery_timeout.reschedule(None)
                    self.db.event(
                        batch_id,
                        "已定位 PDF 入口；下载与校验阶段不计入发现超时",
                        paper_id=paper["id"],
                    )

                await self.acquire_institution_pdf(
                    paper["id"], on_download_started=mark_download_started
                )
        except TimeoutError as exc:
            if not discovery_timeout.expired():
                message = str(exc).strip() or "下载或 PDF 校验阶段超时"
                self.db.update_paper(
                    paper["id"], status="needs_pdf", pdf_status="not_found",
                    needs_action="manual_pdf", error=message,
                )
                self.db.event(
                    batch_id,
                    f"机构自动获取未完成：{message}",
                    level="warning",
                    paper_id=paper["id"],
                )
                return
            seconds = f"{self.institution_discovery_timeout_seconds:g}"
            message = f"机构 PDF 入口发现超过单篇 {seconds} 秒上限，已转入人工队列"
            self.db.update_paper(
                paper["id"], status="needs_pdf", pdf_status="not_found",
                needs_action="manual_pdf", error=message,
            )
            self.db.event(batch_id, message, level="warning", paper_id=paper["id"])
        except LoginTimeoutError:
            raise
        except PublisherBlockedError as exc:
            self.db.update_paper(
                paper["id"], status="needs_pdf", pdf_status="not_found",
                needs_action="manual_pdf", error=str(exc),
            )
            self.db.event(batch_id, f"出版社拒绝，转入人工队列：{exc}", level="warning", paper_id=paper["id"])
        except Exception as exc:
            self.db.update_paper(
                paper["id"], status="needs_pdf", pdf_status="not_found",
                needs_action="manual_pdf", error=str(exc),
            )
            self.db.event(batch_id, f"机构自动获取未完成：{exc}", level="warning", paper_id=paper["id"])

    async def acquire_institution_pdf(
        self,
        paper_id: str,
        *,
        on_download_started: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        self.db.assert_paper_writable(paper_id)
        paper = self.db.get_paper(paper_id)
        if not paper:
            raise KeyError(paper_id)
        if paper.get("metadata_status") != "verified":
            raise ValueError("请先确认题录，再通过机构获取 PDF")
        target = paper.get("source_url")
        if not target:
            doi = paper.get("doi")
            target = self._institution_entry(
                doi=doi,
                fallback=scholar_search_url(paper.get("title") or paper["input_text"]),
            )
        self.db.event(
            paper["batch_id"],
            f"正在使用已登录 Chrome 会话尝试单篇 {self.settings.institution.name} 获取",
            paper_id=paper_id,
        )
        try:
            result = await self.browser.acquire_for_paper(
                paper_id, target, on_download_started=on_download_started
            )
            updated = self.db.get_paper(paper_id)
            if not updated or updated.get("pdf_status") != "verified":
                raise ValueError(updated.get("error") if updated else "PDF 下载后的身份核验未通过")
            self.db.update_paper(
                paper_id, version="published", source_url=result.get("source_url") or target, error=None
            )
            self.db.event(paper["batch_id"], "机构正式版 PDF 已下载并通过身份校验", paper_id=paper_id)
            return result
        except LoginTimeoutError as exc:
            self.db.update_paper(
                paper_id, status="needs_pdf", pdf_status="not_found", needs_action="manual_pdf", error=str(exc)
            )
            self.db.event(paper["batch_id"], f"等待登录超时：{exc}", level="warning", paper_id=paper_id)
            raise
        except Exception as exc:
            self.db.update_paper(
                paper_id, status="needs_pdf", pdf_status="not_found", needs_action="manual_pdf", error=str(exc)
            )
            self.db.event(paper["batch_id"], f"机构自动获取未完成：{exc}", level="warning", paper_id=paper_id)
            raise

    async def open_institution_login(self) -> str:
        url = self.settings.institution.ezproxy_login
        if "{url}" in url:
            url = url.replace("{url}", "https://doi.org/")
        await self.browser.open_for_paper("institution-login", url)
        return url

    async def _browser_downloaded(self, paper_id: str, path: Path) -> None:
        if self.db.is_batch_deleting_for_paper(paper_id):
            return
        if paper_id in self._staged_browser_papers:
            self._staged_browser_downloads.setdefault(paper_id, set()).add(path)
            return
        paper = self.db.get_paper(paper_id)
        if paper and paper.get("pdf_status") in {"verified", "accepted"}:
            existing = Path(paper["pdf_path"]) if paper.get("pdf_path") else None
            if existing is None or existing.resolve() != path.resolve():
                path.unlink(missing_ok=True)
            return
        try:
            await self.accept_local_pdf(paper_id, path)
        except Exception as exc:
            paper = self.db.get_paper(paper_id)
            if paper:
                with suppress(BatchDeletingError):
                    self.db.event(paper["batch_id"], f"浏览器下载文件未通过校验：{exc}", level="error", paper_id=paper_id)

    async def open_paper_url(
        self, paper_id: str, *, scholar: bool = False, resolver: bool = False
    ) -> None:
        self.db.assert_paper_writable(paper_id)
        paper = self.db.get_paper(paper_id)
        if not paper:
            raise KeyError(paper_id)
        if resolver:
            doi = paper.get("doi")
            if not doi:
                raise ValueError("没有 DOI，无法打开机构馆藏解析器")
            url = institution_openurl(self.settings.institution, doi)
            if not url:
                raise ValueError("当前机构没有配置 OpenURL/馆藏解析器")
        elif scholar:
            url = scholar_search_url(paper.get("doi") or paper.get("title") or paper["input_text"])
        else:
            url = paper.get("source_url") or scholar_search_url(paper.get("doi") or paper.get("title") or paper["input_text"])
        await self.browser.open_for_paper(paper_id, url)

    def commit(self, batch_id: str) -> None:
        self.db.assert_batch_writable(batch_id)
        current = self._commit_tasks.get(batch_id)
        if current and not current.done():
            return
        self._commit_tasks[batch_id] = asyncio.create_task(self._commit_batch(batch_id))

    async def _commit_batch(self, batch_id: str) -> None:
        with self.db.allow_deleting_writes():
            async with self._commit_lock:
                batch = self.db.get_batch(batch_id)
                if not batch:
                    return
                await self._commit_zotero_batch(batch)

    async def export_endnote(self, batch_id: str) -> dict[str, Any]:
        self.db.assert_batch_writable(batch_id)
        batch = self.db.get_batch(batch_id)
        if not batch:
            raise KeyError(batch_id)
        collection_name = batch["target_library"].strip()
        destination = downloads_library_dir(collection_name)
        try:
            manifest = await export_zotero_endnote_package(self.zotero, collection_name, destination)
        except Exception as exc:
            self.db.event(batch_id, f"EndNote 导出失败：{exc}", level="error")
            raise
        self.db.update_batch(batch_id, endnote_export_path=str(destination))
        copied = manifest.get("pdf_count", 0)
        self.db.event(
            batch_id,
            f"已从 Zotero 导出 EndNote 导入包：{destination}（{manifest.get('record_count', 0)} 篇，{copied} 个 PDF）",
        )
        return manifest

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
                fields = {
                    "endnote_status": "verified",
                    "status": "complete" if has_full_text else "needs_pdf",
                    "record_number": result.get("record_number"),
                    "needs_action": None if has_full_text else "manual_pdf",
                }
                if has_full_text:
                    fields["error"] = None
                self.db.update_paper(paper["id"], **fields)
                self.db.finish_operation(operation_id, "verified", result)
                self.db.event(batch_id, "Zotero 写入及对账完成", paper_id=paper["id"])
            except Exception as exc:
                self.db.finish_operation(operation_id, "uncertain", {"error": str(exc)})
                self.db.update_paper(
                    paper["id"], endnote_status="uncertain", status="failed",
                    needs_action="reconcile_endnote", error=str(exc),
                )
                self.db.event(batch_id, f"Zotero 提交状态不确定：{exc}", level="error", paper_id=paper["id"])
        self.db.update_batch(batch_id, status="completed", error=None)
        self.db.event(batch_id, "Zotero 提交阶段结束")
