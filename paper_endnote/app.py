from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .inputs import parse_input
from .inputs import normalize_doi
from .pipeline import PipelineManager
from .reporting import batch_csv


settings = Settings.load()
database = Database(settings.database_path)
settings.crossref_mailto = database.get_setting("crossref_email", settings.crossref_mailto)
settings.unpaywall_email = database.get_setting("unpaywall_email", settings.unpaywall_email)
pipeline = PipelineManager(settings, database)
SESSION_TOKEN = secrets.token_urlsafe(32)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await pipeline.close()


app = FastAPI(title="Paper Reference Workflow", version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.app_root / "paper_endnote" / "static"), name="static")


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "").split(":", 1)[0].casefold()
    if host not in {"127.0.0.1", "localhost", "[::1]"}:
        return PlainTextResponse("Local access only", status_code=403)
    origin = request.headers.get("origin")
    if origin and not (origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")):
        return PlainTextResponse("Cross-origin request rejected", status_code=403)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.cookies.get("paper_endnote_session") != SESSION_TOKEN:
            return PlainTextResponse("Invalid local session", status_code=403)
    response = await call_next(request)
    if request.url.path == "/":
        response.set_cookie(
            "paper_endnote_session", SESSION_TOKEN, httponly=True, samesite="strict", secure=False
        )
    return response


class BatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    items_text: str = Field(min_length=1)
    target_library: str = Field(min_length=1)
    library_mode: str = Field(pattern="^(existing|new)$")
    reference_manager: str = Field(default="zotero", pattern="^(zotero|endnote)$")
    start_immediately: bool = True


class MetadataConfirm(BaseModel):
    candidate_id: str | None = None
    metadata: dict[str, Any] | None = None


class DOIUpdate(BaseModel):
    doi: str = Field(min_length=3, max_length=300)


class PDFConfirm(BaseModel):
    accept: bool
    version: str = Field(default="published", pattern="^(published|accepted|preprint|unknown)$")


class SettingsUpdate(BaseModel):
    crossref_email: str = ""
    unpaywall_email: str = ""


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(settings.app_root / "paper_endnote" / "static" / "index.html")


@app.get("/api/state")
async def state() -> dict[str, Any]:
    return {
        "version": "0.3.0",
        "runtime_dir": str(settings.runtime_dir),
        "endnote": pipeline.endnote.probe(),
        "zotero": await pipeline.zotero.probe(),
        "settings": {
            "crossref_email": settings.crossref_mailto,
            "unpaywall_email": settings.unpaywall_email,
        },
        "libraries": discover_libraries(),
    }


@app.post("/api/zotero/authorize")
async def authorize_zotero() -> dict[str, Any]:
    try:
        result = await pipeline.zotero.authorize()
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc
    if not result["remember"]:
        pipeline.zotero.api_key = ""
        raise HTTPException(409, "批处理需要多次写入；请重新授权并选择 Always Allow")
    database.set_setting("zotero_api_key", result["key"])
    return {"status": "authorized", "remember": result["remember"]}


@app.post("/api/settings")
async def update_settings(payload: SettingsUpdate) -> dict[str, str]:
    settings.crossref_mailto = payload.crossref_email.strip()
    settings.unpaywall_email = payload.unpaywall_email.strip()
    database.set_setting("crossref_email", settings.crossref_mailto)
    database.set_setting("unpaywall_email", settings.unpaywall_email)
    return {"status": "saved"}


@app.get("/api/batches")
async def list_batches() -> list[dict[str, Any]]:
    return database.list_batches()


@app.post("/api/batches", status_code=201)
async def create_batch(payload: BatchCreate) -> dict[str, str]:
    items = parse_input(payload.items_text)
    if not items:
        raise HTTPException(400, "没有识别到 DOI 或题名")
    if len(items) > 100:
        raise HTTPException(400, "单批最多 100 篇；建议保持在 20–50 篇")
    if payload.reference_manager == "endnote":
        try:
            target = str(pipeline.endnote.validate_library_path(payload.target_library, payload.library_mode))
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        target = payload.target_library.strip()
        if not target or len(target) > 120 or any(character in target for character in "\\/\0"):
            raise HTTPException(400, "Zotero collection 名称无效")
    batch_id = database.create_batch(
        name=payload.name.strip(), target_library=target, library_mode=payload.library_mode,
        reference_manager=payload.reference_manager, items=items
    )
    if payload.start_immediately:
        pipeline.start(batch_id)
    return {"id": batch_id}


@app.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str) -> dict[str, Any]:
    batch = database.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    return batch


@app.post("/api/batches/{batch_id}/start")
async def start_batch(batch_id: str) -> dict[str, str]:
    require_batch(batch_id)
    pipeline.start(batch_id)
    return {"status": "running"}


@app.post("/api/batches/{batch_id}/pause")
async def pause_batch(batch_id: str) -> dict[str, str]:
    require_batch(batch_id)
    pipeline.pause(batch_id)
    return {"status": "paused"}


@app.post("/api/batches/{batch_id}/resume")
async def resume_batch(batch_id: str) -> dict[str, str]:
    require_batch(batch_id)
    pipeline.resume(batch_id)
    return {"status": "running"}


@app.post("/api/batches/{batch_id}/commit")
async def commit_batch(batch_id: str) -> dict[str, str]:
    batch = require_batch(batch_id)
    if (
        batch.get("reference_manager") != "zotero"
        and batch["library_mode"] == "existing"
        and not batch.get("backup_path")
        and pipeline.endnote.is_endnote_running()
    ):
        raise HTTPException(409, "首次写入前需要备份目标库。请关闭 EndNote，然后再次点击提交。")
    pipeline.commit(batch_id)
    return {"status": "started"}


@app.get("/api/batches/{batch_id}/events")
async def get_events(batch_id: str, after: int = 0) -> list[dict[str, Any]]:
    require_batch(batch_id)
    return database.events_after(batch_id, after)


@app.get("/api/batches/{batch_id}/report.csv")
async def report(batch_id: str) -> Response:
    batch = require_batch(batch_id)
    filename = f"paper-endnote-{batch_id[:8]}.csv"
    return Response(
        batch_csv(batch), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/papers/{paper_id}/candidates")
async def candidates(paper_id: str) -> list[dict[str, Any]]:
    require_paper(paper_id)
    return database.get_candidates(paper_id)


@app.post("/api/papers/{paper_id}/confirm-metadata")
async def confirm_metadata(paper_id: str, payload: MetadataConfirm) -> dict[str, str]:
    require_paper(paper_id)
    metadata = payload.metadata
    if payload.candidate_id:
        selected = next((item for item in database.get_candidates(paper_id) if item["id"] == payload.candidate_id), None)
        if not selected:
            raise HTTPException(404, "候选题录不存在")
        metadata = selected["metadata"]
    if not metadata or not metadata.get("title"):
        raise HTTPException(400, "需要有效题录")
    pipeline.confirm_metadata(paper_id, metadata)
    return {"status": "accepted"}


@app.post("/api/papers/{paper_id}/resolve-doi")
async def resolve_doi(paper_id: str, payload: DOIUpdate) -> dict[str, str]:
    paper = require_paper(paper_id)
    doi = normalize_doi(payload.doi)
    if not doi:
        raise HTTPException(400, "DOI 格式无效")
    if paper.get("endnote_status") == "verified":
        raise HTTPException(409, "该题录已写入目标文献库，不能在此处替换 DOI")
    database.replace_candidates(paper_id, [])
    database.update_paper(
        paper_id,
        input_doi=doi,
        doi=None,
        title=None,
        year=None,
        authors_json=[],
        journal=None,
        metadata_json={},
        metadata_status="pending",
        pdf_status="pending",
        endnote_status="pending",
        status="queued",
        version=None,
        source_url=None,
        pdf_path=None,
        pdf_sha256=None,
        needs_action=None,
        error=None,
    )
    database.event(paper["batch_id"], f"已补充 DOI，重新解析：{doi}", paper_id=paper_id)
    pipeline.start(paper["batch_id"])
    return {"status": "queued", "doi": doi}


@app.post("/api/papers/{paper_id}/retry")
async def retry_paper(paper_id: str) -> dict[str, str]:
    paper = require_paper(paper_id)
    database.reset_paper_for_retry(paper_id)
    pipeline.start(paper["batch_id"])
    return {"status": "queued"}


@app.post("/api/papers/{paper_id}/skip")
async def skip_paper(paper_id: str) -> dict[str, str]:
    paper = require_paper(paper_id)
    database.update_paper(paper_id, status="skipped", needs_action=None, error=None)
    database.event(paper["batch_id"], "已跳过", paper_id=paper_id)
    return {"status": "skipped"}


@app.post("/api/papers/{paper_id}/upload-pdf")
async def upload_pdf(paper_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    require_paper(paper_id)
    if not file.filename or not file.filename.casefold().endswith(".pdf"):
        raise HTTPException(400, "请选择 PDF 文件")
    temporary = settings.runtime_dir / "uploads" / paper_id / Path(file.filename).name
    temporary.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with temporary.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_pdf_bytes:
                    raise HTTPException(413, "PDF 超过 100 MB 限制")
                handle.write(chunk)
        result = await pipeline.accept_local_pdf(paper_id, temporary)
        return result
    finally:
        temporary.unlink(missing_ok=True)


@app.post("/api/papers/{paper_id}/confirm-pdf")
async def confirm_pdf(paper_id: str, payload: PDFConfirm) -> dict[str, str]:
    paper = require_paper(paper_id)
    if not payload.accept:
        if paper.get("pdf_path"):
            Path(paper["pdf_path"]).unlink(missing_ok=True)
        database.update_paper(
            paper_id, pdf_status="rejected", status="needs_pdf", pdf_path=None,
            pdf_sha256=None, needs_action="manual_pdf", error="用户拒绝候选 PDF",
        )
        return {"status": "rejected"}
    if not paper.get("pdf_path"):
        raise HTTPException(400, "没有待确认的 PDF")
    database.update_paper(
        paper_id, pdf_status="accepted", status="endnote_pending", version=payload.version,
        endnote_status="pending", needs_action="commit_endnote", error=None,
    )
    return {"status": "accepted"}


@app.post("/api/papers/{paper_id}/open")
async def open_paper(
    paper_id: str, scholar: bool = False, resolver: bool = False
) -> dict[str, str]:
    require_paper(paper_id)
    try:
        await pipeline.open_paper_url(paper_id, scholar=scholar, resolver=resolver)
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"status": "opened"}


@app.post("/api/papers/{paper_id}/acquire-institution")
async def acquire_institution(paper_id: str) -> dict[str, Any]:
    require_paper(paper_id)
    try:
        result = await pipeline.acquire_institution_pdf(paper_id)
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "verified", **result}


def require_batch(batch_id: str) -> dict[str, Any]:
    batch = database.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    return batch


def require_paper(paper_id: str) -> dict[str, Any]:
    paper = database.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    return paper


def discover_libraries() -> list[str]:
    roots = [Path.home() / "Documents", Path.home() / "Desktop"]
    found: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for current, directories, files in os.walk(root):
            relative_depth = len(Path(current).relative_to(root).parts)
            if relative_depth >= 5:
                directories[:] = []
            directories[:] = [item for item in directories if not item.startswith(".")]
            for name in files:
                if name.casefold().endswith(".enl"):
                    found.add(str((Path(current) / name).resolve()))
            if len(found) >= 100:
                break
    return sorted(found, key=str.casefold)


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper reference-manager local web application")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("For safety, --host must be 127.0.0.1 or localhost")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    uvicorn.run("paper_endnote.app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
