from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .db import Database
from .downloader import safe_filename
from .endnote import EndNoteError, EndNoteExportRecord, build_endnote_export_package, export_filename
from .inputs import normalize_doi
from .zotero import ZoteroAdapter, ZoteroError


class LibraryFilesError(ValueError):
    pass


def _plain(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[\r\n]+", " ", str(value)).strip()


def _author_display_name(author: str) -> str:
    name = str(author or "").strip()
    if not name:
        return ""
    if "," in name:
        last, first = (part.strip() for part in name.split(",", 1))
        return " ".join(part for part in (first, last) if part)
    return name


def bibliographic_filename(metadata: dict[str, Any], fallback: str = "paper") -> str:
    authors = metadata.get("authors") or []
    name = _author_display_name(authors[0]) if authors else ""
    title = re.sub(r"\s+", " ", _plain(metadata.get("title") or "")).strip()
    doi = normalize_doi(metadata.get("doi")) or ""
    if name and title:
        stem = f"{name} - {title}"
    else:
        stem = name or title or (doi.replace("/", "_") if doi else fallback)
    return safe_filename(f"{stem}.pdf")


def unique_destination(directory: Path, filename: str, *, ignore: Path | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / filename
    ignore_resolved = ignore.resolve() if ignore and ignore.exists() else None
    if not destination.exists() or (ignore_resolved and destination.resolve() == ignore_resolved):
        return destination
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".pdf"
    index = 2
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists() or (ignore_resolved and candidate.resolve() == ignore_resolved):
            return candidate
        index += 1


def downloads_library_dir(library_name: str) -> Path:
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(library_name or "").strip()).strip(" .")
    if not raw:
        raise LibraryFilesError("缺少库名，无法创建 Downloads 导出文件夹")
    return resolve_export_dir(str(Path.home() / "Downloads" / raw))


def resolve_export_dir(value: str) -> Path:
    raw = str(value or "").strip().strip('"')
    if not raw:
        raise LibraryFilesError("请填写目标文件夹的绝对路径")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise LibraryFilesError("目标文件夹必须是绝对路径")
    destination = path.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        raise LibraryFilesError(f"目标不是文件夹：{destination}")
    return destination


def paper_metadata(paper: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(paper.get("metadata") or {})
    metadata.update(
        {
            "doi": paper.get("doi") or metadata.get("doi"),
            "title": paper.get("title") or metadata.get("title"),
            "year": paper.get("year") or metadata.get("year"),
            "authors": paper.get("authors") or metadata.get("authors") or [],
            "journal": paper.get("journal") or metadata.get("journal") or "",
        }
    )
    return metadata


def rename_pdf_file(path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise LibraryFilesError(f"PDF 不存在：{source}")
    filename = bibliographic_filename(metadata, source.stem)
    destination = unique_destination(source.parent, filename, ignore=source)
    if destination.resolve() == source.resolve():
        return {
            "renamed": False,
            "path": str(source),
            "filename": source.name,
            "previous": source.name,
        }
    source.replace(destination)
    return {
        "renamed": True,
        "path": str(destination),
        "filename": destination.name,
        "previous": source.name,
    }


def copy_pdf_file(path: Path, destination_dir: Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise LibraryFilesError(f"PDF 不存在：{source}")
    filename = bibliographic_filename(metadata or {}, source.stem) if metadata else safe_filename(source.name)
    destination = unique_destination(destination_dir, filename)
    shutil.copy2(source, destination)
    return {"path": str(destination), "filename": destination.name, "source": str(source)}


def export_endnote_data_pdfs(library: Path, destination_dir: Path) -> dict[str, Any]:
    library = Path(library).expanduser()
    if library.suffix.casefold() != ".enl":
        raise LibraryFilesError("EndNote 库必须是 .enl 文件")
    if not library.is_file():
        raise LibraryFilesError(f"EndNote 库不存在：{library}")
    pdf_root = library.with_suffix(".Data") / "PDF"
    if not pdf_root.is_dir():
        raise LibraryFilesError(f"没有 EndNote 数据区 PDF 目录：{pdf_root}")
    metadata_by_file = _endnote_pdf_metadata(library)
    copied: list[dict[str, Any]] = []
    skipped = 0
    for file in sorted(pdf_root.rglob("*.pdf")):
        if not file.is_file():
            skipped += 1
            continue
        metadata = metadata_by_file.get(file.resolve())
        if metadata:
            result = copy_pdf_file(file, destination_dir, metadata)
        else:
            name = safe_filename(f"{file.parent.name}_{file.name}")
            destination = unique_destination(destination_dir, name)
            shutil.copy2(file, destination)
            result = {"filename": destination.name, "source": str(file), "path": str(destination)}
        copied.append(result)
    return {
        "copied": len(copied),
        "skipped": skipped,
        "destination": str(destination_dir),
        "source": str(pdf_root),
        "files": copied,
    }


def endnote_desktop_running() -> bool:
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq EndNote.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    return "EndNote.exe" in (completed.stdout or "")


def _endnote_sdb(library: Path) -> Path:
    return Path(library).with_suffix(".Data") / "sdb" / "sdb.eni"


def _endnote_pdb(library: Path) -> Path:
    return Path(library).with_suffix(".Data") / "sdb" / "pdb.eni"


def _split_endnote_authors(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[\r\n]+|//", value or "") if part.strip()]


def _metadata_from_endnote_ref(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "authors": _split_endnote_authors(str(data.get("author") or "")),
        "title": data.get("title") or "",
        "year": data.get("year"),
        "doi": data.get("electronic_resource_number"),
        "journal": data.get("secondary_title") or "",
    }


def _rewrite_endnote_path(stored: str, old_name: str, new_name: str) -> str:
    text = str(stored or "")
    if not text or old_name not in text:
        return text
    if text.endswith(old_name):
        return text[: -len(old_name)] + new_name
    return text.replace(old_name, new_name)


def _connect_endnote_db(path: Path, *, writable: bool) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise LibraryFilesError(f"没有 EndNote 21 数据库：{resolved}")
    try:
        if writable:
            connection = sqlite3.connect(resolved, timeout=2.0)
        else:
            connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.OperationalError as exc:
        raise LibraryFilesError("无法打开 EndNote 数据库，请先完全退出 EndNote") from exc
    connection.row_factory = sqlite3.Row
    return connection


def _endnote_pdf_metadata(library: Path) -> dict[Path, dict[str, Any]]:
    sdb = _endnote_sdb(library)
    if not sdb.is_file():
        return {}
    pdf_root = Path(library).with_suffix(".Data") / "PDF"
    mapping: dict[Path, dict[str, Any]] = {}
    connection = _connect_endnote_db(sdb, writable=False)
    try:
        rows = connection.execute(
            """
            SELECT file_res.file_path AS file_path, refs.author AS author, refs.year AS year,
                   refs.title AS title, refs.secondary_title AS secondary_title,
                   refs.electronic_resource_number AS electronic_resource_number
            FROM file_res
            JOIN refs ON refs.id = file_res.refs_id
            """
        ).fetchall()
    except sqlite3.DatabaseError:
        connection.close()
        return {}
    connection.close()
    for row in rows:
        stored = str(row["file_path"] or "").replace("\\", "/")
        name = Path(stored.split("internal-pdf://")[-1]).name
        folder = Path(stored.split("internal-pdf://")[-1]).parent.name
        candidates = []
        if folder and name:
            candidates.append(pdf_root / folder / name)
        if name:
            candidates.extend(pdf_root.rglob(name))
        metadata = _metadata_from_endnote_ref(row)
        for candidate in candidates:
            if candidate.is_file():
                mapping[candidate.resolve()] = metadata
                break
    return mapping


def rename_endnote_pdfs(library: Path, *, require_closed: bool = True) -> dict[str, Any]:
    library = Path(library).expanduser()
    if library.suffix.casefold() != ".enl":
        raise LibraryFilesError("EndNote 库必须是 .enl 文件")
    if not library.is_file():
        raise LibraryFilesError(f"EndNote 库不存在：{library}")
    if require_closed and endnote_desktop_running():
        raise LibraryFilesError("请先完全退出 EndNote，再重命名库内 PDF，否则附件链接会丢失")
    pdf_root = library.with_suffix(".Data") / "PDF"
    if not pdf_root.is_dir():
        raise LibraryFilesError(f"没有 EndNote 数据区 PDF 目录：{pdf_root}")
    sdb = _endnote_sdb(library)
    connection = _connect_endnote_db(sdb, writable=True)
    pdb_connection = None
    pdb = _endnote_pdb(library)
    if pdb.is_file():
        try:
            pdb_connection = _connect_endnote_db(pdb, writable=True)
        except LibraryFilesError:
            pdb_connection = None
    renamed = 0
    skipped = 0
    files: list[dict[str, Any]] = []
    try:
        rows = connection.execute(
            """
            SELECT file_res.rowid AS file_rowid, file_res.refs_id AS refs_id,
                   file_res.file_path AS file_path, refs.author AS author, refs.year AS year,
                   refs.title AS title, refs.secondary_title AS secondary_title,
                   refs.electronic_resource_number AS electronic_resource_number, refs.url AS url
            FROM file_res
            JOIN refs ON refs.id = file_res.refs_id
            """
        ).fetchall()
        for row in rows:
            stored = str(row["file_path"] or "").replace("\\", "/")
            relative = stored.split("internal-pdf://")[-1].lstrip("/")
            source = pdf_root / Path(relative)
            if not source.is_file():
                matches = list(pdf_root.rglob(Path(relative).name)) if Path(relative).name else []
                source = matches[0] if len(matches) == 1 else None
            if source is None or not source.is_file():
                skipped += 1
                continue
            metadata = _metadata_from_endnote_ref(row)
            result = rename_pdf_file(source, metadata)
            files.append({"refs_id": row["refs_id"], **result})
            if not result["renamed"]:
                continue
            new_name = result["filename"]
            old_name = result["previous"]
            new_path = _rewrite_endnote_path(row["file_path"], old_name, new_name)
            connection.execute(
                "UPDATE file_res SET file_path = ? WHERE rowid = ?",
                (new_path, row["file_rowid"]),
            )
            url = row["url"]
            if url and old_name in str(url):
                connection.execute(
                    "UPDATE refs SET url = ? WHERE id = ?",
                    (_rewrite_endnote_path(str(url), old_name, new_name), row["refs_id"]),
                )
            if pdb_connection is not None:
                try:
                    pdb_connection.execute(
                        "UPDATE pdf_index SET subkey = ? WHERE refs_id = ? AND subkey = ?",
                        (new_name, row["refs_id"], old_name),
                    )
                except sqlite3.DatabaseError:
                    pass
            renamed += 1
        connection.commit()
        if pdb_connection is not None:
            pdb_connection.commit()
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        if pdb_connection is not None:
            pdb_connection.rollback()
        raise LibraryFilesError(f"更新 EndNote 附件路径失败：{exc}") from exc
    finally:
        connection.close()
        if pdb_connection is not None:
            pdb_connection.close()
    return {
        "renamed": renamed,
        "skipped": skipped,
        "files": files,
        "source": str(library),
    }


def rename_batch_pdfs(database: Database, batch_id: str) -> dict[str, Any]:
    batch = database.get_batch(batch_id)
    if not batch:
        raise LibraryFilesError("批次不存在")
    renamed = 0
    skipped = 0
    files: list[dict[str, Any]] = []
    for paper in batch["papers"]:
        path = paper.get("pdf_path")
        if not path or paper.get("pdf_status") not in {"verified", "accepted"}:
            skipped += 1
            continue
        result = rename_pdf_file(Path(path), paper_metadata(paper))
        if result["renamed"]:
            database.update_paper(paper["id"], pdf_path=result["path"])
            renamed += 1
        files.append({"paper_id": paper["id"], **result})
    return {"renamed": renamed, "skipped": skipped, "files": files, "source": batch["name"]}


def export_batch_pdfs(database: Database, batch_id: str, destination: Path) -> dict[str, Any]:
    batch = database.get_batch(batch_id)
    if not batch:
        raise LibraryFilesError("批次不存在")
    copied = 0
    skipped = 0
    files: list[dict[str, Any]] = []
    for paper in batch["papers"]:
        path = paper.get("pdf_path")
        if not path or paper.get("pdf_status") not in {"verified", "accepted"} or not Path(path).is_file():
            skipped += 1
            continue
        result = copy_pdf_file(Path(path), destination, paper_metadata(paper))
        copied += 1
        files.append({"paper_id": paper["id"], **result})
    return {
        "copied": copied,
        "skipped": skipped,
        "files": files,
        "destination": str(destination),
        "source": batch["name"],
    }


async def rename_zotero_pdfs(zotero: ZoteroAdapter, collection: str) -> dict[str, Any]:
    renamed = 0
    skipped = 0
    files: list[dict[str, Any]] = []
    async for item in zotero.iter_collection_items(collection):
        key = item.get("data", item).get("key") or item.get("key")
        row = await zotero.export_row(key)
        path = row.get("pdf_path")
        if not path:
            skipped += 1
            continue
        result = rename_pdf_file(Path(path), row["metadata"])
        if result["renamed"] and row.get("attachment_key"):
            try:
                await zotero.rename_attachment_filename(
                    row["attachment_key"], result["filename"], row.get("attachment_version")
                )
            except ZoteroError as exc:
                Path(result["path"]).replace(Path(path))
                raise LibraryFilesError(f"已还原文件名；Zotero 未能更新附件：{exc}") from exc
            renamed += 1
        files.append({"zotero_key": key, **result})
    return {"renamed": renamed, "skipped": skipped, "files": files, "source": collection}


async def export_zotero_pdfs(zotero: ZoteroAdapter, collection: str, destination: Path) -> dict[str, Any]:
    copied = 0
    skipped = 0
    files: list[dict[str, Any]] = []
    async for item in zotero.iter_collection_items(collection):
        key = item.get("data", item).get("key") or item.get("key")
        row = await zotero.export_row(key)
        path = row.get("pdf_path")
        if not path:
            skipped += 1
            continue
        result = copy_pdf_file(Path(path), destination, row["metadata"])
        copied += 1
        files.append({"zotero_key": key, **result})
    return {
        "copied": copied,
        "skipped": skipped,
        "files": files,
        "destination": str(destination),
        "source": collection,
    }


async def export_zotero_endnote_package(
    zotero: ZoteroAdapter, collection: str, destination: Path
) -> dict[str, Any]:
    records: list[EndNoteExportRecord] = []
    rec_number = 1
    async for item in zotero.iter_collection_items(collection):
        data = item.get("data", item)
        item_type = str(data.get("itemType") or "")
        if item_type in {"attachment", "note", "annotation"}:
            continue
        key = data.get("key") or item.get("key")
        if not key:
            continue
        row = await zotero.export_row(key)
        metadata = row.get("metadata") or {}
        source_pdf = row.get("pdf_path")
        folder_id = str(row.get("attachment_key") or key)[:16]
        records.append(
            EndNoteExportRecord(
                rec_number=rec_number,
                metadata=metadata,
                source_pdf=Path(source_pdf) if source_pdf else None,
                folder_id=folder_id,
                filename=export_filename(metadata, key),
                zotero_key=key,
            )
        )
        rec_number += 1
    if not records:
        raise LibraryFilesError("该 Zotero collection 没有可导出的题录")
    try:
        manifest = await asyncio.to_thread(
            build_endnote_export_package,
            records,
            destination,
            collection_name=collection,
            library=None,
        )
    except EndNoteError as exc:
        raise LibraryFilesError(str(exc)) from exc
    return {**manifest, "destination": str(destination), "source": collection}
