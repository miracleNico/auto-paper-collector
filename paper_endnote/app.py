from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable
from weakref import WeakKeyDictionary

import anyio
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .credentials import (
    CredentialError,
    clear_credentials,
    credential_status,
    save_credentials,
)
from .db import BatchDeletingError, Database
from .deletion import BatchDeletionManager
from .endnote import probe_endnote
from .inputs import parse_input
from .inputs import normalize_doi
from .institution_access import NeedsManualInstitutionAction, profile_access_type
from .library_files import (
    LibraryFilesError,
    downloads_library_dir,
    export_batch_pdfs,
    export_endnote_data_pdfs,
    export_zotero_endnote_package,
    export_zotero_pdfs,
    list_batch_pdf_items,
    list_endnote_pdf_items,
    list_zotero_pdf_items,
    rename_batch_pdfs,
    rename_endnote_pdfs,
    rename_zotero_pdfs,
    resolve_export_dir,
    sync_endnote_to_zotero,
)
from .redaction import redact_diagnostic_text
from .ocr import ocr_status
from .pipeline import PipelineManager
from .path_picker import (
    PathPickerError,
    discover_endnote_libraries,
    pick_endnote_library,
)
from .reporting import batch_csv
from .user_config import (
    ALLOWED_SOURCES,
    ConfigError,
    DEFAULT_SOURCES,
    INSTITUTION_ACCESS_TYPES,
    OcrOptions,
    institution_from_payload,
    list_presets,
    load_preset,
    normalize_sources,
)


settings = Settings.load()
database = Database(settings.database_path)
settings.crossref_mailto = database.get_setting("crossref_email", settings.crossref_mailto)
settings.unpaywall_email = database.get_setting("unpaywall_email", settings.unpaywall_email)
library_setting = database.get_setting("endnote_library", str(settings.endnote_library or ""))
settings.endnote_library = Path(library_setting).expanduser() if library_setting.strip() else None
pipeline = PipelineManager(settings, database)
deletion_manager = BatchDeletionManager(settings, database, pipeline)
SESSION_TOKEN = secrets.token_urlsafe(32)
_LIBRARY_TOOL_OPERATION_LOCKS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Lock
] = WeakKeyDictionary()


def library_tool_operation_lock() -> asyncio.Lock:
    """Return one shared mutation lock for the current application event loop."""

    loop = asyncio.get_running_loop()
    lock = _LIBRARY_TOOL_OPERATION_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _LIBRARY_TOOL_OPERATION_LOCKS[loop] = lock
    return lock


async def run_blocking(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run blocking library file work in a worker thread so the event loop stays responsive.

    A thread cannot be interrupted, so the caller's locks and guards must stay held until
    it finishes even if the request is cancelled. anyio's shield absorbs cancel-scope
    cancellation without busy re-delivery; asyncio.shield covers a native Task.cancel(),
    which is re-raised once the thread is done.
    """
    work = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    with anyio.CancelScope(shield=True):
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    pass
            raise


@asynccontextmanager
async def lifespan(_: FastAPI):
    await deletion_manager.recover()
    yield
    await deletion_manager.close()
    await pipeline.close()


app = FastAPI(title="Paper Reference Workflow", version=__version__, lifespan=lifespan)
app.state.shutdown_callback = None
app.state.shutdown_requested = False
app.mount("/static", StaticFiles(directory=settings.app_root / "paper_endnote" / "static"), name="static")


@app.exception_handler(BatchDeletingError)
async def batch_deleting_error(_: Request, exc: BatchDeletingError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


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
            # Usually a tab left open across a service restart; the UI re-fetches
            # "/" to pick up the new cookie and retries once on this code.
            return JSONResponse(
                status_code=403,
                content={"detail": "本地会话已失效，请刷新页面", "code": "session_expired"},
            )
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
    reference_manager: str = Field(default="zotero", pattern="^zotero$")
    start_immediately: bool = True


class MetadataConfirm(BaseModel):
    candidate_id: str | None = None
    metadata: dict[str, Any] | None = None


class DOIUpdate(BaseModel):
    doi: str = Field(min_length=3, max_length=300)


class PDFConfirm(BaseModel):
    accept: bool
    version: str = Field(default="published", pattern="^(published|accepted|preprint|unknown)$")


class InstitutionUpdate(BaseModel):
    id: str = ""
    name: str = ""
    access_type: str = Field(default="ezproxy", pattern="^(ezproxy|carsi_saml|manual_browser)$")
    login_url: str = ""
    login_url_markers: list[str] = Field(default_factory=list)
    openurl: str = ""
    ezproxy_login: str = ""
    ezproxy_hosts: list[str] = Field(default_factory=list)
    school_aliases: list[str] = Field(default_factory=list)
    entity_id: str = ""
    publisher_login_urls: dict[str, str] = Field(default_factory=dict)
    preset: str = ""


class InstitutionContext(BaseModel):
    publisher: str = ""
    target_url: str = ""
    batch_id: str = ""


class SettingsUpdate(BaseModel):
    crossref_email: str = ""
    unpaywall_email: str = ""
    endnote_library: str = ""
    acquisition_sources: list[str] = Field(default_factory=lambda: list(DEFAULT_SOURCES))
    ocr_enabled: bool = True
    ocr_languages: str = "eng"
    ocr_max_pages: int = 2
    institution_preset: str = ""
    institution: InstitutionUpdate | None = None
    auto_institution: bool = False
    auto_commit: bool = True
    login_wait_seconds: int = 600


class CredentialsUpdate(BaseModel):
    username: str = ""
    password: str = ""


class LibraryToolRequest(BaseModel):
    source: str = Field(pattern="^(batch|zotero|endnote)$")
    batch_id: str = ""
    collection: str = ""
    collection_key: str = Field(default="", max_length=64)
    whole_library: bool = False
    zotero_library_id: str = Field(default="user:0", max_length=64)
    zotero_library_name: str = Field(default="", max_length=512)
    endnote_library: str = Field(default="", max_length=32767)
    rename_scheme: str = Field(
        default="year_author_title",
        pattern="^(year_author_title|title_only)$",
    )
    deduplicate_pdfs: bool = False
    destination: str = ""
    open_folder: bool = False
    item_ids: list[str] | None = Field(default=None, max_length=10000)


class EndNoteLibraryPickerRequest(BaseModel):
    initial_path: str = Field(default="", max_length=32767)


class EndNoteExportToolRequest(BaseModel):
    collection: str = ""
    destination: str = ""
    open_folder: bool = False


class EndNoteZoteroSyncRequest(BaseModel):
    collection: str = Field(min_length=1, max_length=120)


class BatchDeleteRequest(BaseModel):
    batch_ids: list[str] = Field(min_length=1, max_length=100)


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(settings.app_root / "paper_endnote" / "static" / "index.html")


@app.get("/api/state")
async def state() -> dict[str, Any]:
    return {
        "version": __version__,
        "runtime_dir": str(settings.runtime_dir),
        "config_path": str(settings.config_path),
        "endnote": probe_endnote(settings.endnote_exe, settings.endnote_library),
        "zotero": await pipeline.zotero.probe(),
        "ocr": ocr_status(),
        "institution_access_types": list(INSTITUTION_ACCESS_TYPES),
        "institution_sessions": pipeline.institution_session_states(),
        "presets": [profile.as_dict() for profile in list_presets().values()],
        "credentials": credential_status(settings.institution.id),
        "settings": {
            "crossref_email": settings.crossref_mailto,
            "unpaywall_email": settings.unpaywall_email,
            "endnote_library": str(settings.endnote_library) if settings.endnote_library else "",
            "acquisition_sources": list(settings.acquisition_sources),
            "ocr_enabled": settings.ocr.enabled,
            "ocr_languages": settings.ocr.languages,
            "ocr_max_pages": settings.ocr.max_pages,
            "auto_institution": settings.auto_institution,
            "auto_commit": settings.auto_commit,
            "login_wait_seconds": settings.login_wait_seconds,
            "institution": settings.institution.as_dict(),
        },
    }


@app.post("/api/system/shutdown")
async def shutdown_local_service() -> dict[str, str]:
    callback = getattr(app.state, "shutdown_callback", None)
    if not callable(callback):
        raise HTTPException(409, "当前启动方式不支持从网页关闭；请在终端结束服务")
    if not app.state.shutdown_requested:
        app.state.shutdown_requested = True
        asyncio.get_running_loop().call_later(0.25, callback)
    return {"status": "shutting_down", "message": "本地服务正在关闭"}


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
    provided = payload.model_fields_set
    acquisition_fields = {
        "acquisition_sources",
        "ocr_enabled",
        "ocr_languages",
        "ocr_max_pages",
        "institution_preset",
        "institution",
        "auto_institution",
        "auto_commit",
        "login_wait_seconds",
    }
    try:
        institution_touched = bool(
            "institution_preset" in provided or "institution" in provided
        )
        if "institution_preset" in provided and payload.institution_preset:
            institution = load_preset(payload.institution_preset)
        elif "institution" in provided and payload.institution:
            institution_data = settings.institution.as_dict()
            for field_name in payload.institution.model_fields_set:
                institution_data[field_name] = getattr(payload.institution, field_name)
            if (
                "access_type" in payload.institution.model_fields_set
                and payload.institution.access_type != "ezproxy"
            ):
                if "ezproxy_login" not in payload.institution.model_fields_set:
                    institution_data["ezproxy_login"] = ""
                if "ezproxy_hosts" not in payload.institution.model_fields_set:
                    institution_data["ezproxy_hosts"] = []
            if (
                "access_type" in payload.institution.model_fields_set
                and payload.institution.access_type != "carsi_saml"
            ):
                if "school_aliases" not in payload.institution.model_fields_set:
                    institution_data["school_aliases"] = []
                if "entity_id" not in payload.institution.model_fields_set:
                    institution_data["entity_id"] = ""
                if "publisher_login_urls" not in payload.institution.model_fields_set:
                    institution_data["publisher_login_urls"] = {}
            institution = institution_from_payload(institution_data)
        else:
            institution = settings.institution
        institution_specified = bool(institution.id)
        sources = (
            normalize_sources(payload.acquisition_sources)
            if "acquisition_sources" in provided
            else (
                ALLOWED_SOURCES if institution_specified else DEFAULT_SOURCES
            )
            if institution_touched
            else settings.acquisition_sources
        )
        auto_institution = (
            payload.auto_institution
            if "auto_institution" in provided
            else institution_specified
            if institution_touched
            else settings.auto_institution
        )
        if not institution_specified and (
            "institution" in sources or auto_institution
        ):
            raise ConfigError("启用机构获取前，请先填写机构 ID 和访问方式")
        ocr_languages = settings.ocr.languages
        if "ocr_languages" in provided:
            ocr_languages = payload.ocr_languages.strip() or "eng"
        ocr_max_pages = settings.ocr.max_pages
        if "ocr_max_pages" in provided:
            ocr_max_pages = max(1, min(int(payload.ocr_max_pages), 5))
        ocr = OcrOptions(
            enabled=payload.ocr_enabled if "ocr_enabled" in provided else settings.ocr.enabled,
            languages=ocr_languages,
            max_pages=ocr_max_pages,
        )
        if provided & acquisition_fields:
            settings.ocr = ocr
            settings.acquisition_sources = sources
            settings.institution = institution
            settings.auto_institution = auto_institution
            if "auto_commit" in provided:
                settings.auto_commit = payload.auto_commit
            if "login_wait_seconds" in provided:
                settings.login_wait_seconds = max(30, min(int(payload.login_wait_seconds), 1800))
            settings.save_acquisition_config()
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    if "crossref_email" in provided:
        settings.crossref_mailto = payload.crossref_email.strip()
        database.set_setting("crossref_email", settings.crossref_mailto)
    if "unpaywall_email" in provided:
        settings.unpaywall_email = payload.unpaywall_email.strip()
        database.set_setting("unpaywall_email", settings.unpaywall_email)
    if "endnote_library" in provided:
        library = payload.endnote_library.strip()
        settings.endnote_library = Path(library).expanduser() if library else None
        database.set_setting(
            "endnote_library", str(settings.endnote_library) if settings.endnote_library else ""
        )
    return {"status": "saved"}


@app.post("/api/credentials")
async def update_credentials(payload: CredentialsUpdate) -> dict[str, Any]:
    if not settings.institution.id:
        raise HTTPException(400, "请先保存机构配置")
    if profile_access_type(settings.institution) != "ezproxy":
        raise HTTPException(400, "CARSI 和手动模式不会保存账号或密码")
    try:
        save_credentials(settings.institution.id, payload.username, payload.password)
    except CredentialError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "saved", **credential_status(settings.institution.id)}


@app.post("/api/credentials/clear")
async def delete_credentials() -> dict[str, Any]:
    clear_credentials(settings.institution.id)
    return {"status": "cleared", **credential_status(settings.institution.id)}


@app.get("/api/tools/sources")
async def tool_sources() -> dict[str, Any]:
    collections: list[dict[str, Any]] = []
    zotero_libraries: list[dict[str, Any]] = [
        {"id": "user:0", "name": "My Library", "type": "user"}
    ]
    zotero_error = None
    zotero_library_error = None
    try:
        collections = await pipeline.zotero.list_collections(library_id="user:0")
    except Exception as exc:
        zotero_error = str(exc)
    if zotero_error is None:
        try:
            zotero_libraries = await pipeline.zotero.list_libraries()
        except Exception as exc:
            zotero_library_error = str(exc)
    library = settings.endnote_library
    endnote_libraries = discover_endnote_libraries(library)
    return {
        "batches": [
            {
                "id": batch["id"],
                "name": batch["name"],
                "target_library": batch["target_library"],
                "total": batch.get("total") or 0,
            }
            for batch in database.list_batches()
        ],
        "collections": collections,
        "zotero_libraries": zotero_libraries,
        "zotero_error": zotero_error,
        "zotero_library_error": zotero_library_error,
        "endnote_library": str(library) if library else "",
        "endnote_libraries": endnote_libraries,
        "endnote_data_dir": str(library.with_suffix(".Data") / "PDF") if library else "",
        "downloads_dir": str(Path.home() / "Downloads"),
    }


@app.get("/api/tools/zotero-collections")
async def tool_zotero_collections(library_id: str = "user:0") -> dict[str, Any]:
    try:
        normalized = pipeline.zotero.normalize_library_id(library_id)
        collections = await pipeline.zotero.list_collections(library_id=normalized)
        return {"library_id": normalized, "collections": collections}
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tools/pick-endnote-library")
async def tool_pick_endnote_library(payload: EndNoteLibraryPickerRequest) -> dict[str, Any]:
    try:
        path = await asyncio.to_thread(pick_endnote_library, payload.initial_path)
    except PathPickerError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"path": path, "cancelled": not bool(path)}


def selected_endnote_library(payload: LibraryToolRequest) -> Path:
    selected = payload.endnote_library.strip().strip('"')
    if selected:
        library = Path(selected).expanduser()
        if not library.is_absolute():
            raise LibraryFilesError("EndNote 库必须使用 .enl 文件的绝对路径")
        return library
    if settings.endnote_library:
        return Path(settings.endnote_library).expanduser()
    raise LibraryFilesError("请选择 EndNote .enl 库")


@app.post("/api/tools/rename-pdfs")
async def tool_rename_pdfs(payload: LibraryToolRequest) -> dict[str, Any]:
    async with library_tool_operation_lock():
        try:
            if payload.source == "batch":
                if not payload.batch_id.strip():
                    raise LibraryFilesError("请选择本机批次")
                batch_id = payload.batch_id.strip()
                require_batch(batch_id, writable=True)
                async with pipeline.track_batch_work(batch_id, cancellable=False):
                    async with pipeline.zotero_operation_guard():
                        return await run_blocking(
                            rename_batch_pdfs,
                            database,
                            batch_id,
                            naming_scheme=payload.rename_scheme,
                            deduplicate_pdfs=payload.deduplicate_pdfs,
                        )
            if payload.source == "zotero":
                async with pipeline.zotero_operation_guard():
                    return await rename_zotero_pdfs(
                        pipeline.zotero,
                        payload.collection.strip(),
                        library_id=payload.zotero_library_id.strip() or "user:0",
                        collection_key=payload.collection_key.strip(),
                        whole_library=payload.whole_library,
                        naming_scheme=payload.rename_scheme,
                        deduplicate_pdfs=payload.deduplicate_pdfs,
                    )
            if payload.source == "endnote":
                return await run_blocking(
                    rename_endnote_pdfs,
                    selected_endnote_library(payload),
                    naming_scheme=payload.rename_scheme,
                    deduplicate_pdfs=payload.deduplicate_pdfs,
                )
            raise LibraryFilesError("未知数据区")
        except LibraryFilesError as exc:
            raise HTTPException(400, str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc


def selected_tool_item_ids(payload: LibraryToolRequest) -> set[str] | None:
    if payload.item_ids is None:
        return None
    normalized = {str(item).strip() for item in payload.item_ids if str(item).strip()}
    if not normalized:
        raise LibraryFilesError("请至少选择一篇论文")
    return normalized


@app.post("/api/tools/export-pdfs/candidates")
async def tool_export_pdf_candidates(payload: LibraryToolRequest) -> dict[str, Any]:
    try:
        if payload.source == "batch":
            batch_id = payload.batch_id.strip()
            if not batch_id:
                raise LibraryFilesError("请选择本机批次")
            batch = require_batch(batch_id)
            items = await run_blocking(list_batch_pdf_items, database, batch_id)
            source_name = batch["name"]
        elif payload.source == "zotero":
            collection = payload.collection.strip()
            items = await list_zotero_pdf_items(
                pipeline.zotero,
                collection,
                library_id=payload.zotero_library_id.strip() or "user:0",
                collection_key=payload.collection_key.strip(),
                whole_library=payload.whole_library,
            )
            source_name = (
                collection
                or payload.zotero_library_name.strip()
                or payload.zotero_library_id.strip()
                or "My Library"
            )
        elif payload.source == "endnote":
            library = selected_endnote_library(payload)
            items = await run_blocking(list_endnote_pdf_items, library)
            source_name = library.stem
        else:
            raise LibraryFilesError("未知数据区")
        return {
            "source": payload.source,
            "source_name": source_name,
            "items": items,
            "available_count": sum(1 for item in items if item["available"]),
            "available_size": sum(int(item["size"]) for item in items if item["available"]),
        }
    except LibraryFilesError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/tools/export-pdfs")
async def tool_export_pdfs(payload: LibraryToolRequest) -> dict[str, Any]:
    async with library_tool_operation_lock():
        try:
            item_ids = selected_tool_item_ids(payload)
            if payload.source == "batch":
                if not payload.batch_id.strip():
                    raise LibraryFilesError("请选择本机批次")
                batch_id = payload.batch_id.strip()
                batch = require_batch(batch_id, writable=True)
                lib_name = batch["target_library"]
                destination = resolve_export_dir(payload.destination) if payload.destination.strip() else downloads_library_dir(lib_name)
                async with pipeline.track_batch_work(batch_id, cancellable=False):
                    result = await run_blocking(
                        export_batch_pdfs, database, batch_id, destination, item_ids=item_ids
                    )
            elif payload.source == "zotero":
                source_name = (
                    payload.collection.strip()
                    or payload.zotero_library_name.strip()
                    or payload.zotero_library_id.strip()
                    or "My Library"
                )
                destination = (
                    resolve_export_dir(payload.destination)
                    if payload.destination.strip()
                    else downloads_library_dir(source_name)
                )
                async with pipeline.zotero_operation_guard():
                    result = await export_zotero_pdfs(
                        pipeline.zotero,
                        payload.collection.strip(),
                        destination,
                        item_ids=item_ids,
                        library_id=payload.zotero_library_id.strip() or "user:0",
                        collection_key=payload.collection_key.strip(),
                        whole_library=payload.whole_library,
                    )
            elif payload.source == "endnote":
                library = selected_endnote_library(payload)
                destination = (
                    resolve_export_dir(payload.destination)
                    if payload.destination.strip()
                    else downloads_library_dir(library.stem)
                )
                result = await run_blocking(
                    export_endnote_data_pdfs, library, destination, item_ids=item_ids
                )
            else:
                raise LibraryFilesError("未知数据区")
            if payload.open_folder:
                os.startfile(destination)  # noqa: S606 - local Windows helper
            return result
        except LibraryFilesError as exc:
            raise HTTPException(400, str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc


@app.post("/api/tools/export-endnote")
async def tool_export_endnote(payload: EndNoteExportToolRequest) -> dict[str, Any]:
    async with library_tool_operation_lock():
        try:
            collection = payload.collection.strip()
            if not collection:
                raise LibraryFilesError("请选择 Zotero collection")
            destination = (
                resolve_export_dir(payload.destination)
                if payload.destination.strip()
                else downloads_library_dir(collection)
            )
            async with pipeline.zotero_operation_guard():
                result = await export_zotero_endnote_package(
                    pipeline.zotero, collection, destination
                )
            if payload.open_folder:
                os.startfile(destination)  # noqa: S606 - local Windows helper
            return result
        except LibraryFilesError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc


@app.post("/api/tools/sync-endnote-zotero")
async def tool_sync_endnote_zotero(payload: EndNoteZoteroSyncRequest) -> dict[str, Any]:
    async with library_tool_operation_lock():
        try:
            library = settings.endnote_library
            if not library:
                raise LibraryFilesError("请先在设置中填写 EndNote 库路径")
            async with pipeline.zotero_operation_guard():
                async with pipeline.zotero.sync_guard():
                    return await sync_endnote_to_zotero(
                        pipeline.zotero,
                        library,
                        payload.collection.strip(),
                    )
        except LibraryFilesError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc


@app.post("/api/institution/login")
async def institution_login(
    payload: InstitutionContext | None = None,
) -> dict[str, Any]:
    context = payload or InstitutionContext()
    try:
        return await pipeline.open_institution_login(
            target_url=context.target_url.strip() or None,
            publisher=context.publisher.strip().casefold() or None,
            batch_id=context.batch_id.strip() or None,
        )
    except Exception as exc:
        raise HTTPException(500, redact_diagnostic_text(exc)) from exc


@app.post("/api/institution/test-access")
async def institution_test_access(
    payload: InstitutionContext | None = None,
) -> dict[str, Any]:
    context = payload or InstitutionContext()
    try:
        return await pipeline.test_institution_access(
            target_url=context.target_url.strip() or None,
            publisher=context.publisher.strip().casefold() or None,
        )
    except Exception as exc:
        raise HTTPException(409, redact_diagnostic_text(exc)) from exc


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
    target = payload.target_library.strip()
    if not target or len(target) > 120 or any(character in target for character in "\\/\0"):
        raise HTTPException(400, "Zotero collection 名称无效")
    batch_id = database.create_batch(
        name=payload.name.strip(), target_library=target, library_mode=payload.library_mode,
        reference_manager="zotero", items=items,
        institution_config={
            **settings.institution.as_dict(),
            "_acquisition_sources": list(settings.acquisition_sources),
            "_auto_institution": bool(settings.auto_institution),
        },
    )
    if payload.start_immediately:
        pipeline.start(batch_id)
    return {"id": batch_id}


@app.post("/api/batches/delete-preview")
async def preview_batch_deletion(payload: BatchDeleteRequest) -> dict[str, Any]:
    return deletion_manager.preview(normalize_batch_ids(payload.batch_ids))


@app.post("/api/batches/delete", status_code=202)
async def delete_batches(payload: BatchDeleteRequest) -> dict[str, Any]:
    operation = deletion_manager.request(normalize_batch_ids(payload.batch_ids))
    return {"operation_id": operation["id"], **operation}


@app.get("/api/batch-deletions/{operation_id}")
async def get_batch_deletion(operation_id: str) -> dict[str, Any]:
    operation = deletion_manager.get(operation_id)
    if not operation:
        raise HTTPException(404, "删除操作不存在")
    return operation


@app.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str) -> dict[str, Any]:
    batch = database.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    return batch


@app.post("/api/batches/{batch_id}/start")
async def start_batch(batch_id: str) -> dict[str, str]:
    require_batch(batch_id, writable=True)
    pipeline.start(batch_id)
    return {"status": "running"}


@app.post("/api/batches/{batch_id}/pause")
async def pause_batch(batch_id: str) -> dict[str, str]:
    require_batch(batch_id, writable=True)
    pipeline.pause(batch_id)
    return {"status": "paused"}


@app.post("/api/batches/{batch_id}/resume")
async def resume_batch(batch_id: str) -> dict[str, str]:
    require_batch(batch_id, writable=True)
    pipeline.resume(batch_id)
    return {"status": "running"}


@app.post("/api/batches/{batch_id}/commit")
async def commit_batch(batch_id: str) -> dict[str, str]:
    require_batch(batch_id, writable=True)
    pipeline.commit(batch_id)
    return {"status": "started"}


@app.post("/api/batches/{batch_id}/export-endnote")
async def export_endnote(batch_id: str) -> dict[str, Any]:
    require_batch(batch_id, writable=True)
    try:
        async with pipeline.track_batch_work(batch_id, cancellable=False):
            return await pipeline.export_endnote(batch_id)
    except Exception as exc:
        raise HTTPException(409, redact_diagnostic_text(exc)) from exc


@app.get("/api/batches/{batch_id}/endnote-export.zip")
async def download_endnote_export(batch_id: str) -> FileResponse:
    batch = require_batch(batch_id)
    export_dir = Path(batch["endnote_export_path"]) if batch.get("endnote_export_path") else None
    zip_path = None
    if export_dir:
        inside = export_dir / "endnote-export.zip"
        sibling = export_dir.with_suffix(".zip")
        if inside.is_file():
            zip_path = inside
        elif sibling.is_file():
            zip_path = sibling
    if not zip_path or not zip_path.is_file():
        raise HTTPException(404, "还没有 EndNote 导入包。请先在工具页从 Zotero 导出。")
    return FileResponse(zip_path, filename=f"endnote-export-{batch_id[:8]}.zip", media_type="application/zip")


@app.post("/api/batches/{batch_id}/open-endnote-export")
async def open_endnote_export(batch_id: str) -> dict[str, str]:
    batch = require_batch(batch_id)
    export_dir = Path(batch["endnote_export_path"]) if batch.get("endnote_export_path") else None
    if not export_dir or not export_dir.is_dir():
        raise HTTPException(404, "还没有 EndNote 导入包")
    os.startfile(export_dir)  # noqa: S606 - local Windows helper
    return {"status": "opened", "path": str(export_dir)}


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
    require_paper(paper_id, writable=True)
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
    paper = require_paper(paper_id, writable=True)
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
        institution_publisher=None,
        institution_state=None,
        needs_action=None,
        error=None,
    )
    database.event(paper["batch_id"], f"已补充 DOI，重新解析：{doi}", paper_id=paper_id)
    pipeline.start(paper["batch_id"])
    return {"status": "queued", "doi": doi}


@app.post("/api/papers/{paper_id}/retry")
async def retry_paper(paper_id: str) -> dict[str, str]:
    paper = require_paper(paper_id, writable=True)
    database.reset_paper_for_retry(paper_id)
    pipeline.start(paper["batch_id"])
    return {"status": "queued"}


@app.post("/api/papers/{paper_id}/skip")
async def skip_paper(paper_id: str) -> dict[str, str]:
    paper = require_paper(paper_id, writable=True)
    database.update_paper(paper_id, status="skipped", needs_action=None, error=None)
    database.event(paper["batch_id"], "已跳过", paper_id=paper_id)
    return {"status": "skipped"}


@app.post("/api/papers/{paper_id}/upload-pdf")
async def upload_pdf(paper_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    paper = require_paper(paper_id, writable=True)
    if not file.filename or not file.filename.casefold().endswith(".pdf"):
        raise HTTPException(400, "请选择 PDF 文件")
    async with pipeline.track_batch_work(paper["batch_id"], cancellable=False):
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
    paper = require_paper(paper_id, writable=True)
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
    paper = require_paper(paper_id, writable=True)
    try:
        async with pipeline.track_batch_work(paper["batch_id"], cancellable=True):
            await pipeline.open_paper_url(paper_id, scholar=scholar, resolver=resolver)
    except BatchDeletingError:
        raise
    except Exception as exc:
        raise HTTPException(500, redact_diagnostic_text(exc)) from exc
    return {"status": "opened"}


@app.post("/api/papers/{paper_id}/acquire-institution")
async def acquire_institution(paper_id: str) -> dict[str, Any]:
    paper = require_paper(paper_id, writable=True)
    try:
        async with pipeline.track_batch_work(paper["batch_id"], cancellable=True):
            result = await pipeline.acquire_institution_pdf(paper_id)
    except NeedsManualInstitutionAction as exc:
        return {"status": "waiting", "action": exc.as_dict()}
    except Exception as exc:
        raise HTTPException(409, redact_diagnostic_text(exc)) from exc
    return {"status": "verified", **result}


@app.post("/api/papers/{paper_id}/continue-institution")
async def continue_institution(paper_id: str) -> dict[str, Any]:
    paper = require_paper(paper_id, writable=True)
    try:
        async with pipeline.track_batch_work(paper["batch_id"], cancellable=True):
            return await pipeline.continue_institution_pdf(paper_id)
    except Exception as exc:
        raise HTTPException(409, redact_diagnostic_text(exc)) from exc


def normalize_batch_ids(batch_ids: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(item.strip() for item in batch_ids if item.strip()))
    if not normalized:
        raise HTTPException(400, "请选择要删除的批次")
    return normalized


def require_batch(batch_id: str, *, writable: bool = False) -> dict[str, Any]:
    batch = database.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "批次不存在")
    if writable:
        database.assert_batch_writable(batch_id)
    return batch


def require_paper(paper_id: str, *, writable: bool = False) -> dict[str, Any]:
    paper = database.get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    if writable:
        database.assert_paper_writable(paper_id)
    return paper


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
    config = uvicorn.Config(app, host=args.host, port=args.port, reload=False)
    server = uvicorn.Server(config)
    app.state.shutdown_requested = False
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    try:
        server.run()
    finally:
        app.state.shutdown_callback = None


if __name__ == "__main__":
    main()
