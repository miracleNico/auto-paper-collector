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
from .endnote import (
    EndNoteError,
    EndNoteExportRecord,
    build_endnote_export_package,
    export_filename,
    resolve_internal_attachment,
)
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


def _reserve_unique_destination(directory: Path, filename: str) -> Path:
    """Atomically reserve a new export path owned by the current operation."""

    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".pdf"
    index = 1
    while True:
        candidate = directory / (filename if index == 1 else f"{stem}_{index}{suffix}")
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            index += 1
            continue
        try:
            os.close(descriptor)
        except BaseException:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return candidate


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
    destination = _reserve_unique_destination(destination_dir, filename)
    _copy_export_file(source, destination)
    return {"path": str(destination), "filename": destination.name, "source": str(source)}


def _copy_export_file(source: Path, destination: Path) -> None:
    """Copy one export file without leaving a partial destination on failure."""

    try:
        shutil.copy2(source, destination)
    except BaseException:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _rollback_export_files(paths: list[Path]) -> None:
    """Best-effort removal of files created by the current export attempt."""

    for path in reversed(paths):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _confined_endnote_pdf_files(pdf_root: Path) -> list[Path]:
    """Return real PDFs whose resolved targets remain inside the EndNote PDF root."""

    root = pdf_root.resolve()
    files: list[Path] = []
    seen: set[Path] = set()
    for path in sorted(pdf_root.rglob("*.pdf")):
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if (
            not resolved.is_relative_to(root)
            or resolved in seen
            or not resolved.is_file()
            or resolved.suffix.casefold() != ".pdf"
        ):
            continue
        seen.add(resolved)
        files.append(path)
    return files


def export_endnote_data_pdfs(
    library: Path, destination_dir: Path, *, item_ids: set[str] | None = None
) -> dict[str, Any]:
    library = _validate_endnote_library(library)
    if item_ids is not None and not item_ids:
        raise LibraryFilesError("请至少选择一篇论文")
    pdf_root = library.with_suffix(".Data") / "PDF"
    if not pdf_root.is_dir():
        raise LibraryFilesError(f"没有 EndNote 数据区 PDF 目录：{pdf_root}")
    if item_ids is not None:
        item_map = _endnote_pdf_item_map(library)
        unknown = item_ids - set(item_map)
        if unknown:
            raise LibraryFilesError("所选 EndNote 论文已变化，请刷新后重试")
        copied: list[dict[str, Any]] = []
        created_paths: list[Path] = []
        skipped = 0
        try:
            for identifier in sorted(item_ids):
                metadata, paths = item_map[identifier]
                existing = [Path(path) for path in paths if Path(path).is_file()]
                if not existing:
                    skipped += 1
                    continue
                for path in existing:
                    result = copy_pdf_file(path, destination_dir, metadata)
                    created_paths.append(Path(result["path"]))
                    copied.append({"item_id": identifier, **result})
        except BaseException:
            _rollback_export_files(created_paths)
            raise
        return {
            "copied": len(copied),
            "skipped": skipped,
            "selected": len(item_ids),
            "destination": str(destination_dir),
            "source": str(pdf_root),
            "files": copied,
        }
    metadata_by_file = _endnote_pdf_metadata(library)
    copied: list[dict[str, Any]] = []
    created_paths: list[Path] = []
    skipped = 0
    try:
        for file in _confined_endnote_pdf_files(pdf_root):
            metadata = metadata_by_file.get(file.resolve())
            if metadata:
                result = copy_pdf_file(file, destination_dir, metadata)
            else:
                name = safe_filename(f"{file.parent.name}_{file.name}")
                destination = _reserve_unique_destination(destination_dir, name)
                _copy_export_file(file, destination)
                result = {"filename": destination.name, "source": str(file), "path": str(destination)}
            created_paths.append(Path(result["path"]))
            copied.append(result)
    except BaseException:
        _rollback_export_files(created_paths)
        raise
    return {
        "copied": len(copied),
        "skipped": skipped,
        "selected": None,
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
    except (OSError, RuntimeError):
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
    year_text = str(data.get("year") or "")
    year_match = re.search(r"\b(?:18|19|20|21)\d{2}\b", year_text)
    tags = [
        part.strip()
        for part in re.split(r"[\r\n]+|//|;", str(data.get("keywords") or ""))
        if part.strip()
    ]
    raw_reference_type = data.get("reference_type")
    try:
        reference_type = int(raw_reference_type) if raw_reference_type is not None else None
    except (TypeError, ValueError):
        reference_type = None
    return {
        "authors": _split_endnote_authors(str(data.get("author") or "")),
        "title": data.get("title") or "",
        "year": int(year_match.group(0)) if year_match else None,
        "doi": normalize_doi(data.get("electronic_resource_number")),
        "journal": data.get("secondary_title") or "",
        "volume": data.get("volume") or "",
        "issue": data.get("number") or "",
        "pages": data.get("pages") or "",
        "url": data.get("url") or "",
        "abstract": data.get("abstract") or "",
        "short_title": data.get("short_title") or "",
        "language": data.get("language") or "",
        "access_date": data.get("access_date") or "",
        "tags": tags,
        "publisher": data.get("publisher") or "",
        "isbn": data.get("isbn") or "",
        "endnote_reference_type": reference_type,
        "item_type": "journalArticle" if reference_type == 0 else None,
        "library_catalog": "EndNote",
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


def _validate_endnote_library(library: Path) -> Path:
    library = Path(library).expanduser()
    if library.suffix.casefold() != ".enl":
        raise LibraryFilesError("EndNote 库必须是 .enl 文件")
    try:
        library = library.resolve(strict=True)
    except OSError:
        raise LibraryFilesError(f"EndNote 库不存在：{library}")
    if not library.is_file():
        raise LibraryFilesError(f"EndNote 库不存在：{library}")
    return library


def _resolve_endnote_pdf(library: Path, stored_path: str) -> Path | None:
    stored = str(stored_path or "").strip().replace("\\", "/")
    if not stored:
        return None
    pdf_root = library.with_suffix(".Data") / "PDF"
    confined_to_pdf_root = stored.casefold().startswith("internal-pdf://")
    candidate = resolve_internal_attachment(library, stored)
    if (
        candidate is None
        and not stored.casefold().startswith("file:")
        and not re.match(r"^[A-Za-z]:/", stored)
    ):
        relative = Path(stored.replace("/", os.sep))
        if not relative.is_absolute():
            candidate = pdf_root / relative
            confined_to_pdf_root = True
    if candidate:
        candidate = candidate.expanduser().resolve()
        if confined_to_pdf_root:
            root = pdf_root.resolve()
            if not candidate.is_relative_to(root):
                candidate = None
        if candidate and candidate.is_file() and candidate.suffix.casefold() == ".pdf":
            return candidate
    return None


def read_endnote_library_records(library: Path) -> list[dict[str, Any]]:
    """Read active EndNote references and their PDF attachments without modifying the library."""

    library = _validate_endnote_library(library)
    sdb = _endnote_sdb(library)
    connection = _connect_endnote_db(sdb, writable=False)
    wanted = (
        "id",
        "trash_state",
        "reference_type",
        "author",
        "year",
        "title",
        "pages",
        "secondary_title",
        "volume",
        "number",
        "url",
        "abstract",
        "keywords",
        "short_title",
        "language",
        "access_date",
        "publisher",
        "isbn",
        "electronic_resource_number",
    )
    try:
        ref_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(refs)")}
        if "id" not in ref_columns:
            raise LibraryFilesError("EndNote 数据库缺少题录 ID")
        expressions = [
            f'"{name}" AS "{name}"' if name in ref_columns else f'NULL AS "{name}"'
            for name in wanted
        ]
        where = "WHERE COALESCE(trash_state, 0)=0" if "trash_state" in ref_columns else ""
        refs = connection.execute(
            f"SELECT {', '.join(expressions)} FROM refs {where} ORDER BY id"
        ).fetchall()
        file_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(file_res)")}
        files = []
        if {"refs_id", "file_path"}.issubset(file_columns):
            files = connection.execute(
                "SELECT refs_id, file_path FROM file_res ORDER BY refs_id, file_pos"
                if "file_pos" in file_columns
                else "SELECT refs_id, file_path FROM file_res ORDER BY refs_id"
            ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise LibraryFilesError(f"读取 EndNote 数据库失败：{exc}") from exc
    finally:
        connection.close()

    attachments: dict[str, list[Path]] = {}
    for row in files:
        ref_id = str(row["refs_id"])
        path = _resolve_endnote_pdf(library, str(row["file_path"] or ""))
        if path and path not in attachments.setdefault(ref_id, []):
            attachments[ref_id].append(path)

    records: list[dict[str, Any]] = []
    for row in refs:
        ref_id = str(row["id"])
        records.append(
            {
                "id": f"ref:{ref_id}",
                "ref_id": ref_id,
                "metadata": _metadata_from_endnote_ref(row),
                "pdf_paths": attachments.get(ref_id, []),
            }
        )
    return records


def _pdf_item(identifier: str, metadata: dict[str, Any], paths: list[Path]) -> dict[str, Any]:
    existing = [Path(path) for path in paths if Path(path).is_file()]
    authors = metadata.get("authors") or []
    return {
        "id": identifier,
        "title": metadata.get("title") or (existing[0].stem if existing else "未命名论文"),
        "authors": authors,
        "author": authors[0] if authors else "",
        "year": metadata.get("year"),
        "doi": normalize_doi(metadata.get("doi")),
        "available": bool(existing),
        "file_count": len(existing),
        "size": sum(path.stat().st_size for path in existing),
        "filename": existing[0].name if existing else "",
    }


def _endnote_pdf_item_map(
    library: Path,
) -> dict[str, tuple[dict[str, Any], list[Path]]]:
    library = _validate_endnote_library(library)
    records: list[dict[str, Any]] = []
    try:
        records = read_endnote_library_records(library)
    except LibraryFilesError:
        if _endnote_sdb(library).is_file():
            raise
    item_map: dict[str, tuple[dict[str, Any], list[Path]]] = {
        record["id"]: (record["metadata"], record["pdf_paths"])
        for record in records
    }
    known = {Path(path).resolve() for record in records for path in record["pdf_paths"]}
    pdf_root = library.with_suffix(".Data") / "PDF"
    if not pdf_root.is_dir():
        raise LibraryFilesError(f"没有 EndNote 数据区 PDF 目录：{pdf_root}")
    for path in _confined_endnote_pdf_files(pdf_root):
        resolved = path.resolve()
        if resolved in known:
            continue
        relative = path.relative_to(pdf_root).as_posix()
        item_map[f"file:{relative}"] = ({"title": path.stem}, [path])
    return item_map


def list_endnote_pdf_items(library: Path) -> list[dict[str, Any]]:
    return [
        _pdf_item(identifier, metadata, paths)
        for identifier, (metadata, paths) in _endnote_pdf_item_map(library).items()
    ]


def _endnote_pdf_metadata(library: Path) -> dict[Path, dict[str, Any]]:
    sdb = _endnote_sdb(library)
    if not sdb.is_file():
        return {}
    pdf_root = Path(library).with_suffix(".Data") / "PDF"
    pdf_files = _confined_endnote_pdf_files(pdf_root) if pdf_root.is_dir() else []
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
            ORDER BY file_res.rowid
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
            candidates.extend(path for path in pdf_files if path.name == name)
        metadata = _metadata_from_endnote_ref(row)
        for candidate in candidates:
            if candidate.is_file():
                mapping[candidate.resolve()] = metadata
                break
    return mapping


def rename_endnote_pdfs(library: Path, *, require_closed: bool = True) -> dict[str, Any]:
    library = _validate_endnote_library(library)
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
    moved_files: list[tuple[Path, Path]] = []
    primary_committed = False
    try:
        rows = connection.execute(
            """
            SELECT file_res.rowid AS file_rowid, file_res.refs_id AS refs_id,
                   file_res.file_path AS file_path, refs.author AS author, refs.year AS year,
                   refs.title AS title, refs.secondary_title AS secondary_title,
                   refs.electronic_resource_number AS electronic_resource_number, refs.url AS url
            FROM file_res
            JOIN refs ON refs.id = file_res.refs_id
            ORDER BY file_res.rowid
            """
        ).fetchall()
        resolved_pdf_root = pdf_root.resolve()
        attachment_rows: dict[Path, list[sqlite3.Row]] = {}
        current_urls: dict[int, str] = {}
        for row in rows:
            stored = str(row["file_path"] or "").replace("\\", "/")
            if not stored.casefold().startswith("internal-pdf://"):
                skipped += 1
                continue
            candidate = resolve_internal_attachment(library, stored)
            source = candidate.resolve() if candidate is not None else None
            if source is None or not source.is_relative_to(resolved_pdf_root):
                skipped += 1
                continue
            if not source.is_file():
                matches = (
                    [
                        match.resolve()
                        for match in pdf_root.rglob(source.name)
                        if match.resolve().is_relative_to(resolved_pdf_root)
                    ]
                    if source.name
                    else []
                )
                source = matches[0] if len(matches) == 1 else None
            if source is None or not source.is_file():
                skipped += 1
                continue
            if source.suffix.casefold() != ".pdf":
                skipped += 1
                continue
            attachment_rows.setdefault(source, []).append(row)

        for source, shared_rows in attachment_rows.items():
            metadata = _metadata_from_endnote_ref(shared_rows[0])
            result = rename_pdf_file(source, metadata)
            reference_ids = [row["refs_id"] for row in shared_rows]
            files.append(
                {
                    "refs_id": reference_ids[0],
                    "refs_ids": reference_ids,
                    **result,
                }
            )
            if not result["renamed"]:
                continue
            moved_files.append((Path(result["path"]), source))
            new_name = result["filename"]
            old_name = result["previous"]
            for row in shared_rows:
                stored = str(row["file_path"] or "").replace("\\", "/")
                stored_name = Path(stored.split("internal-pdf://", 1)[-1]).name
                path_name = stored_name or old_name
                new_path = _rewrite_endnote_path(
                    row["file_path"], path_name, new_name
                )
                connection.execute(
                    "UPDATE file_res SET file_path = ? WHERE rowid = ?",
                    (new_path, row["file_rowid"]),
                )
                refs_id = int(row["refs_id"])
                url = current_urls.get(refs_id, str(row["url"] or ""))
                if url and path_name in url:
                    updated_url = _rewrite_endnote_path(url, path_name, new_name)
                    connection.execute(
                        "UPDATE refs SET url = ? WHERE id = ?",
                        (
                            updated_url,
                            refs_id,
                        ),
                    )
                    current_urls[refs_id] = updated_url
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
        primary_committed = True
        if pdb_connection is not None:
            try:
                pdb_connection.commit()
            except sqlite3.DatabaseError:
                # The PDB is a secondary PDF index. Keep the authoritative SDB
                # and files consistent even if EndNote needs to rebuild it.
                pdb_connection.rollback()
    except BaseException as exc:
        rollback_errors: list[str] = []
        if not primary_committed:
            try:
                connection.rollback()
            except sqlite3.DatabaseError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if pdb_connection is not None:
            try:
                pdb_connection.rollback()
            except sqlite3.DatabaseError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if not primary_committed:
            for destination, original in reversed(moved_files):
                try:
                    if destination.exists() and not original.exists():
                        destination.replace(original)
                    elif destination.exists() and original.exists():
                        rollback_errors.append(f"原文件与新文件同时存在：{original}")
                except OSError as rollback_exc:
                    rollback_errors.append(f"{destination} -> {original}: {rollback_exc}")
        if rollback_errors:
            raise LibraryFilesError(
                "EndNote 重命名失败，且无法完全还原文件："
                + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, sqlite3.DatabaseError):
            raise LibraryFilesError(f"更新 EndNote 附件路径失败：{exc}") from exc
        raise
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


def list_batch_pdf_items(database: Database, batch_id: str) -> list[dict[str, Any]]:
    batch = database.get_batch(batch_id)
    if not batch:
        raise LibraryFilesError("批次不存在")
    items = []
    for paper in batch["papers"]:
        path = Path(paper["pdf_path"]) if paper.get("pdf_path") else None
        available = bool(
            path
            and paper.get("pdf_status") in {"verified", "accepted"}
            and path.is_file()
        )
        items.append(
            _pdf_item(
                paper["id"],
                paper_metadata(paper),
                [path] if available and path else [],
            )
        )
    return items


def export_batch_pdfs(
    database: Database,
    batch_id: str,
    destination: Path,
    *,
    item_ids: set[str] | None = None,
) -> dict[str, Any]:
    batch = database.get_batch(batch_id)
    if not batch:
        raise LibraryFilesError("批次不存在")
    if item_ids is not None and not item_ids:
        raise LibraryFilesError("请至少选择一篇论文")
    known_ids = {paper["id"] for paper in batch["papers"]}
    if item_ids is not None and item_ids - known_ids:
        raise LibraryFilesError("所选批次论文已变化，请刷新后重试")
    copied = 0
    skipped = 0
    files: list[dict[str, Any]] = []
    created_paths: list[Path] = []
    try:
        for paper in batch["papers"]:
            if item_ids is not None and paper["id"] not in item_ids:
                continue
            path = paper.get("pdf_path")
            if not path or paper.get("pdf_status") not in {"verified", "accepted"} or not Path(path).is_file():
                skipped += 1
                continue
            result = copy_pdf_file(Path(path), destination, paper_metadata(paper))
            created_paths.append(Path(result["path"]))
            copied += 1
            files.append({"paper_id": paper["id"], **result})
    except BaseException:
        _rollback_export_files(created_paths)
        raise
    return {
        "copied": copied,
        "skipped": skipped,
        "selected": len(item_ids) if item_ids is not None else None,
        "files": files,
        "destination": str(destination),
        "source": batch["name"],
    }


async def _iter_zotero_source_items(
    zotero: ZoteroAdapter,
    collection: str,
    *,
    library_id: str,
    collection_key: str,
    whole_library: bool,
):
    """Iterate an explicitly scoped Zotero source, with legacy name lookup support."""

    if whole_library:
        if collection_key or collection:
            raise LibraryFilesError("Zotero 整库范围不能同时指定 collection")
        async for item in zotero.iter_library_items(
            library_id=library_id,
            collection_key="",
        ):
            yield item
        return
    if collection_key:
        async for item in zotero.iter_library_items(
            library_id=library_id,
            collection_key=collection_key,
        ):
            yield item
        return
    if not collection:
        raise LibraryFilesError("请选择 Zotero collection，或明确选择整个库")
    if library_id != "user:0":
        async for item in zotero.iter_collection_items(collection, library_id=library_id):
            yield item
        return
    async for item in zotero.iter_collection_items(collection):
        yield item


async def rename_zotero_pdfs(
    zotero: ZoteroAdapter,
    collection: str,
    *,
    library_id: str = "user:0",
    collection_key: str = "",
    whole_library: bool = False,
) -> dict[str, Any]:
    renamed = 0
    skipped = 0
    files: list[dict[str, Any]] = []
    warnings: list[str] = []
    scoped = bool(collection_key or whole_library or library_id != "user:0")
    source_items = _iter_zotero_source_items(
        zotero,
        collection,
        library_id=library_id,
        collection_key=collection_key,
        whole_library=whole_library,
    )

    def failure_message(message: str) -> str:
        if renamed:
            return f"操作部分完成：已成功重命名 {renamed} 个文件；{message}"
        return message

    iterator = aiter(source_items)
    while True:
        try:
            item = await anext(iterator)
        except StopAsyncIteration:
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LibraryFilesError(
                failure_message(f"读取 Zotero 范围失败：{exc}")
            ) from exc
        data = item.get("data", item)
        if str(data.get("itemType") or "") in {"attachment", "note", "annotation"}:
            continue
        key = data.get("key") or item.get("key")
        if not key:
            continue
        try:
            row = (
                await zotero.export_row(key, library_id=library_id)
                if scoped
                else await zotero.export_row(key)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LibraryFilesError(
                failure_message(f"读取 Zotero 条目 {key} 失败：{exc}")
            ) from exc
        path = row.get("pdf_path")
        if not path:
            skipped += 1
            continue
        try:
            result = rename_pdf_file(Path(path), row["metadata"])
        except Exception as exc:
            raise LibraryFilesError(
                failure_message(f"重命名 Zotero 条目 {key} 的文件失败：{exc}")
            ) from exc
        if result["renamed"]:
            renamed_path = Path(result["path"])
            original_path = Path(path)

            def restore_original() -> None:
                if original_path.exists():
                    raise LibraryFilesError(
                        failure_message(
                            "无法安全还原 Zotero 文件名：原路径已重新出现；已保留两个文件"
                        )
                    )
                if not renamed_path.exists():
                    raise LibraryFilesError(
                        failure_message("无法安全还原 Zotero 文件名：新路径也已不存在")
                    )
                try:
                    renamed_path.replace(original_path)
                except OSError as rollback_exc:
                    raise LibraryFilesError(
                        failure_message(
                            "Zotero 附件更新失败，且无法还原本地文件名："
                            f"{rollback_exc}"
                        )
                    ) from rollback_exc

            if not row.get("attachment_key"):
                restore_original()
                raise LibraryFilesError(
                    failure_message("Zotero 附件缺少标识，已还原本地文件名")
                )
            warning = ""
            try:
                if scoped:
                    await zotero.rename_attachment_filename(
                        row["attachment_key"],
                        result["filename"],
                        row.get("attachment_version"),
                        library_id=library_id,
                    )
                else:
                    await zotero.rename_attachment_filename(
                        row["attachment_key"], result["filename"], row.get("attachment_version")
                    )
            except BaseException as exc:
                if isinstance(exc, ZoteroError):
                    restore_original()
                    raise LibraryFilesError(
                        failure_message(f"已还原文件名；Zotero 未能更新附件：{exc}")
                    ) from exc

                committed_filename = ""
                try:
                    if scoped:
                        committed_filename = await asyncio.shield(
                            zotero.attachment_filename(
                                row["attachment_key"], library_id=library_id
                            )
                        )
                    else:
                        committed_filename = await asyncio.shield(
                            zotero.attachment_filename(row["attachment_key"])
                        )
                except BaseException:
                    committed_filename = ""

                normalized_committed = committed_filename.casefold()
                if normalized_committed == result["filename"].casefold():
                    # The PATCH reached Zotero even though its response was
                    # lost. Keep the new filename so the attachment stays valid.
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    warning = "Zotero 已提交新文件名，但更新响应丢失；已通过回查确认"
                elif normalized_committed in {
                    original_path.name.casefold(),
                    str(result["previous"]).casefold(),
                }:
                    restore_original()
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise LibraryFilesError(
                        failure_message(
                            f"已还原当前文件名；Zotero 未能确认附件更新：{exc}"
                        )
                    ) from exc
                else:
                    try:
                        if renamed_path.exists() and not original_path.exists():
                            shutil.copy2(renamed_path, original_path)
                    except OSError as rollback_exc:
                        raise LibraryFilesError(
                            failure_message(
                                "Zotero 附件更新结果不确定，且无法保留兼容副本："
                                f"{rollback_exc}"
                            )
                        ) from exc
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise LibraryFilesError(
                        failure_message(
                            "Zotero 附件更新结果无法确认；为避免断链，已暂时保留新旧两个文件名"
                        )
                    ) from exc
            if warning:
                warnings.append(f"{key}: {warning}")
            renamed += 1
        files.append({"zotero_key": key, **result})
    return {
        "renamed": renamed,
        "skipped": skipped,
        "files": files,
        "warnings": warnings,
        "source": collection or collection_key or library_id,
    }


async def list_zotero_pdf_items(
    zotero: ZoteroAdapter,
    collection: str,
    *,
    library_id: str = "user:0",
    collection_key: str = "",
    whole_library: bool = False,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    scoped = bool(collection_key or whole_library or library_id != "user:0")
    async for item in _iter_zotero_source_items(
        zotero,
        collection,
        library_id=library_id,
        collection_key=collection_key,
        whole_library=whole_library,
    ):
        data = item.get("data", item)
        if str(data.get("itemType") or "") in {"attachment", "note", "annotation"}:
            continue
        key = data.get("key") or item.get("key")
        if not key:
            continue
        row = (
            await zotero.export_row(key, library_id=library_id)
            if scoped
            else await zotero.export_row(key)
        )
        path = Path(row["pdf_path"]) if row.get("pdf_path") else None
        items.append(_pdf_item(key, row.get("metadata") or {}, [path] if path else []))
    return items


async def export_zotero_pdfs(
    zotero: ZoteroAdapter,
    collection: str,
    destination: Path,
    *,
    item_ids: set[str] | None = None,
    library_id: str = "user:0",
    collection_key: str = "",
    whole_library: bool = False,
) -> dict[str, Any]:
    if item_ids is not None and not item_ids:
        raise LibraryFilesError("请至少选择一篇论文")
    copied = 0
    skipped = 0
    files: list[dict[str, Any]] = []
    known_ids: set[str] = set()
    source_items: list[tuple[str, dict[str, Any]]] = []
    scoped = bool(collection_key or whole_library or library_id != "user:0")
    async for item in _iter_zotero_source_items(
        zotero,
        collection,
        library_id=library_id,
        collection_key=collection_key,
        whole_library=whole_library,
    ):
        data = item.get("data", item)
        if str(data.get("itemType") or "") in {"attachment", "note", "annotation"}:
            continue
        key = data.get("key") or item.get("key")
        if not key:
            continue
        known_ids.add(key)
        source_items.append((key, item))
    if item_ids is not None and item_ids - known_ids:
        raise LibraryFilesError("所选 Zotero 论文已变化，请刷新后重试")
    created_paths: list[Path] = []
    try:
        for key, _item in source_items:
            if item_ids is not None and key not in item_ids:
                continue
            row = (
                await zotero.export_row(key, library_id=library_id)
                if scoped
                else await zotero.export_row(key)
            )
            path = row.get("pdf_path")
            if not path:
                skipped += 1
                continue
            result = copy_pdf_file(Path(path), destination, row["metadata"])
            created_paths.append(Path(result["path"]))
            copied += 1
            files.append({"zotero_key": key, **result})
    except BaseException:
        _rollback_export_files(created_paths)
        raise
    return {
        "copied": copied,
        "skipped": skipped,
        "selected": len(item_ids) if item_ids is not None else None,
        "files": files,
        "destination": str(destination),
        "source": collection or collection_key or library_id,
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


async def sync_endnote_to_zotero(
    zotero: ZoteroAdapter, library: Path, collection: str
) -> dict[str, Any]:
    """Copy active EndNote references into one Zotero collection with exact-match deduplication."""

    collection = str(collection or "").strip()
    if not collection:
        raise LibraryFilesError("请填写目标 Zotero collection")
    if endnote_desktop_running():
        raise LibraryFilesError("请先完全退出 EndNote，再同步到 Zotero")
    records = read_endnote_library_records(library)
    if not records:
        raise LibraryFilesError("EndNote 库中没有可同步的题录")
    try:
        collection_key = await zotero.ensure_collection(collection, create=True)
    except ZoteroError as exc:
        raise LibraryFilesError(str(exc)) from exc

    created = 0
    matched = 0
    synced = 0
    skipped = 0
    failed = 0
    pdf_attached = 0
    pdf_existing = 0
    errors: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []
    for record in records:
        metadata = record["metadata"]
        title = str(metadata.get("title") or "").strip()
        doi = normalize_doi(metadata.get("doi"))
        if not metadata.get("item_type"):
            skipped += 1
            reference_type = metadata.get("endnote_reference_type")
            reference_label = str(reference_type) if reference_type is not None else "未知"
            rows.append(
                {
                    "id": record["id"],
                    "status": "skipped",
                    "title": title,
                    "error": f"暂不支持的 EndNote 题录类型：{reference_label}",
                }
            )
            continue
        if not title and not doi:
            skipped += 1
            rows.append({"id": record["id"], "status": "skipped", "title": ""})
            continue
        pdf_paths = [Path(path) for path in record["pdf_paths"] if Path(path).is_file()]
        try:
            result = await zotero.commit_paper(
                collection_key,
                metadata,
                None,
            )
            created += int(bool(result.get("created")))
            matched += int(not bool(result.get("created")))
            for path in pdf_paths:
                attachment = await zotero.attach_pdf(result["record_number"], path)
                pdf_attached += int(bool(attachment.get("attached")))
                pdf_existing += int(bool(attachment.get("existing")))
            synced += 1
            rows.append(
                {
                    "id": record["id"],
                    "status": "synced",
                    "title": title,
                    "zotero_key": result["record_number"],
                    "pdf_count": len(pdf_paths),
                }
            )
        except Exception as exc:
            failed += 1
            message = str(exc)
            errors.append({"id": record["id"], "title": title or doi or record["id"], "error": message})
            rows.append({"id": record["id"], "status": "failed", "title": title, "error": message})
    return {
        "source": str(Path(library)),
        "collection": collection,
        "collection_key": collection_key,
        "record_count": len(records),
        "synced": synced,
        "created": created,
        "matched": matched,
        "skipped": skipped,
        "failed": failed,
        "pdf_attached": pdf_attached,
        "pdf_existing": pdf_existing,
        "errors": errors,
        "records": rows,
    }
