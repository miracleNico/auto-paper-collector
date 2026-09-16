from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .config import Settings
from .db import Database

if TYPE_CHECKING:
    from .pipeline import PipelineManager


_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class UnsafeDeletionPath(RuntimeError):
    pass


class BatchCleanupError(RuntimeError):
    def __init__(self, message: str, *, deleted_bytes: int = 0):
        super().__init__(message)
        self.deleted_bytes = deleted_bytes


@dataclass(frozen=True)
class CleanupTarget:
    path: Path
    bytes: int


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _paper_directory(root: Path, paper_id: str) -> Path:
    if (
        not paper_id
        or Path(paper_id).name != paper_id
        or paper_id in {".", ".."}
        or "/" in paper_id
        or "\\" in paper_id
    ):
        raise UnsafeDeletionPath(f"不安全的论文目录标识：{paper_id!r}")
    root_path = root.absolute()
    candidate = root_path / paper_id
    if candidate.parent != root_path:
        raise UnsafeDeletionPath(f"清理路径越界：{candidate}")
    return candidate


def _checked_tree_size(path: Path) -> int:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if _is_reparse(info):
        raise UnsafeDeletionPath(f"拒绝清理重解析路径：{path}")
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeDeletionPath(f"论文清理目标不是目录：{path}")
    total = 0
    with os.scandir(path) as entries:
        for entry in entries:
            child = Path(entry.path)
            child_info = entry.stat(follow_symlinks=False)
            if _is_reparse(child_info):
                raise UnsafeDeletionPath(f"拒绝清理重解析路径：{child}")
            if stat.S_ISDIR(child_info.st_mode):
                total += _checked_tree_size(child)
            elif stat.S_ISREG(child_info.st_mode):
                total += child_info.st_size
            else:
                raise UnsafeDeletionPath(f"拒绝清理特殊文件：{child}")
    return total


def _validated_target(root: Path, paper_id: str) -> CleanupTarget | None:
    candidate = _paper_directory(root, paper_id)
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return None
    if _is_reparse(info):
        raise UnsafeDeletionPath(f"拒绝清理重解析路径：{candidate}")
    root_resolved = root.resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    if resolved.parent != root_resolved:
        raise UnsafeDeletionPath(f"清理路径越界：{candidate}")
    return CleanupTarget(candidate, _checked_tree_size(candidate))


def measure_batch_files(settings: Settings, paper_ids: list[str]) -> tuple[int, str | None]:
    total = 0
    try:
        for paper_id in paper_ids:
            for root in (settings.download_dir, settings.runtime_dir / "uploads"):
                target = _validated_target(root, paper_id)
                if target:
                    total += target.bytes
    except (OSError, UnsafeDeletionPath) as exc:
        return total, str(exc)
    return total, None


def _remove_checked_tree(path: Path) -> int:
    info = path.lstat()
    if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeDeletionPath(f"拒绝清理不安全目录：{path}")
    deleted = 0
    with os.scandir(path) as entries:
        children = list(entries)
    for entry in children:
        child = Path(entry.path)
        child_info = child.lstat()
        if _is_reparse(child_info):
            raise UnsafeDeletionPath(f"拒绝清理重解析路径：{child}")
        if stat.S_ISDIR(child_info.st_mode):
            deleted += _remove_checked_tree(child)
        elif stat.S_ISREG(child_info.st_mode):
            size = child_info.st_size
            child.unlink()
            deleted += size
        else:
            raise UnsafeDeletionPath(f"拒绝清理特殊文件：{child}")
    path.rmdir()
    return deleted


def cleanup_batch_files(settings: Settings, paper_ids: list[str]) -> int:
    """Remove only per-paper download/upload trees after validating every target."""
    targets: list[CleanupTarget] = []
    try:
        for paper_id in paper_ids:
            for root in (settings.download_dir, settings.runtime_dir / "uploads"):
                target = _validated_target(root, paper_id)
                if target:
                    targets.append(target)
    except (OSError, UnsafeDeletionPath) as exc:
        raise BatchCleanupError(str(exc)) from exc

    deleted = 0
    for target in targets:
        try:
            deleted += _remove_checked_tree(target.path)
        except (OSError, UnsafeDeletionPath) as exc:
            raise BatchCleanupError(str(exc), deleted_bytes=deleted) from exc
    return deleted


class BatchDeletionManager:
    def __init__(self, settings: Settings, database: Database, pipeline: PipelineManager):
        self.settings = settings
        self.db = database
        self.pipeline = pipeline
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def preview(self, batch_ids: list[str]) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(batch_ids))
        batches: list[dict[str, Any]] = []
        for batch_id in unique_ids:
            batch = self.db.get_batch(batch_id)
            if not batch:
                batches.append(
                    {
                        "id": batch_id,
                        "name": batch_id[:8],
                        "paper_count": 0,
                        "bytes": 0,
                        "running": False,
                        "deletion_status": "missing",
                        "missing": True,
                        "error": None,
                    }
                )
                continue
            paper_ids = [paper["id"] for paper in batch["papers"]]
            size, error = measure_batch_files(self.settings, paper_ids)
            batches.append(
                {
                    "id": batch_id,
                    "name": batch["name"],
                    "paper_count": len(paper_ids),
                    "bytes": size,
                    "running": self.pipeline.is_batch_running(batch_id)
                    or batch["status"] == "running",
                    "deletion_status": batch.get("deletion_status") or "active",
                    "missing": False,
                    "error": error,
                }
            )
        return {
            "batches": batches,
            "totals": {
                "batch_count": len(batches),
                "paper_count": sum(item["paper_count"] for item in batches),
                "bytes": sum(item["bytes"] for item in batches),
            },
        }

    def request(self, batch_ids: list[str]) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(batch_ids))
        existing = self.db.find_active_deletion_operation(unique_ids)
        if existing:
            return existing
        preview = self.preview(unique_ids)
        operation_id = self.db.create_deletion_operation(preview["batches"])
        self._schedule(operation_id)
        operation = self.db.get_deletion_operation(operation_id)
        assert operation is not None
        return operation

    def get(self, operation_id: str) -> dict[str, Any] | None:
        return self.db.get_deletion_operation(operation_id)

    async def recover(self) -> None:
        for operation in self.db.list_recoverable_deletions():
            self._schedule(operation["id"])

    async def close(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule(self, operation_id: str) -> None:
        current = self._tasks.get(operation_id)
        if current and not current.done():
            return
        task = asyncio.create_task(self._run(operation_id))
        self._tasks[operation_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(operation_id, None))

    async def _run(self, operation_id: str) -> None:
        self.db.set_deletion_operation_status(operation_id, "running")
        operation = self.db.get_deletion_operation(operation_id)
        if not operation:
            return
        for item in operation["batches"]:
            if item["status"] in {"completed", "missing", "failed"}:
                continue
            await self._delete_batch(operation_id, item["batch_id"])
        self.db.finalize_deletion_operation(operation_id)

    async def _delete_batch(self, operation_id: str, batch_id: str) -> None:
        batch = self.db.get_batch(batch_id)
        if not batch:
            self.db.set_deletion_batch_status(operation_id, batch_id, "missing")
            return
        paper_ids = [paper["id"] for paper in batch["papers"]]
        self.db.set_deletion_batch_status(operation_id, batch_id, "stopping")
        try:
            await self.pipeline.prepare_batch_deletion(batch_id, paper_ids)
            self.db.set_deletion_batch_status(operation_id, batch_id, "deleting")
            deleted_bytes = await asyncio.to_thread(
                cleanup_batch_files, self.settings, paper_ids
            )
            self.db.delete_batch_transaction(
                operation_id, batch_id, deleted_bytes=deleted_bytes
            )
        except BatchCleanupError as exc:
            self.db.set_deletion_batch_status(
                operation_id,
                batch_id,
                "failed",
                deleted_bytes=exc.deleted_bytes,
                error=str(exc),
            )
        except Exception as exc:
            self.db.set_deletion_batch_status(
                operation_id, batch_id, "failed", error=str(exc)
            )
