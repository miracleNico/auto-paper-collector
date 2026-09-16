from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


class BatchDeletingError(RuntimeError):
    """Raised when a write targets a batch whose deletion has started."""


_allow_deleting_writes: ContextVar[bool] = ContextVar(
    "paper_endnote_allow_deleting_writes", default=False
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        schema = """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            target_library TEXT NOT NULL,
            library_mode TEXT NOT NULL CHECK(library_mode IN ('existing','new')),
            status TEXT NOT NULL DEFAULT 'draft',
            paused INTEGER NOT NULL DEFAULT 0,
            backup_path TEXT,
            endnote_export_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT,
            deletion_status TEXT NOT NULL DEFAULT 'active',
            deletion_operation_id TEXT,
            deletion_error TEXT
        );
        CREATE TABLE IF NOT EXISTS papers (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            input_text TEXT NOT NULL,
            input_doi TEXT,
            input_title TEXT,
            input_year INTEGER,
            input_author TEXT,
            doi TEXT,
            title TEXT,
            year INTEGER,
            authors_json TEXT NOT NULL DEFAULT '[]',
            journal TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            metadata_status TEXT NOT NULL DEFAULT 'pending',
            pdf_status TEXT NOT NULL DEFAULT 'pending',
            endnote_status TEXT NOT NULL DEFAULT 'pending',
            status TEXT NOT NULL DEFAULT 'queued',
            version TEXT,
            source_url TEXT,
            pdf_path TEXT,
            pdf_sha256 TEXT,
            record_number TEXT,
            needs_action TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(batch_id, position)
        );
        CREATE INDEX IF NOT EXISTS ix_papers_batch_status ON papers(batch_id, status, position);
        CREATE INDEX IF NOT EXISTS ix_papers_doi ON papers(batch_id, doi);
        CREATE TABLE IF NOT EXISTS candidates (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            score REAL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operations (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            step TEXT NOT NULL,
            status TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_operations_paper ON operations(paper_id, started_at);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
            paper_id TEXT,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_events_batch_id ON events(batch_id, id);
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deletion_operations (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS deletion_batches (
            operation_id TEXT NOT NULL REFERENCES deletion_operations(id) ON DELETE CASCADE,
            batch_id TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            paper_count INTEGER NOT NULL DEFAULT 0,
            estimated_bytes INTEGER NOT NULL DEFAULT 0,
            deleted_bytes INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(operation_id, batch_id)
        );
        CREATE INDEX IF NOT EXISTS ix_deletion_batches_batch
            ON deletion_batches(batch_id, created_at);
        """
        with self._lock, self.connect() as connection:
            connection.executescript(schema)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(batches)")}
            if "reference_manager" not in columns:
                connection.execute(
                    "ALTER TABLE batches ADD COLUMN reference_manager TEXT NOT NULL DEFAULT 'zotero'"
                )
            if "endnote_export_path" not in columns:
                connection.execute("ALTER TABLE batches ADD COLUMN endnote_export_path TEXT")
            if "deletion_status" not in columns:
                connection.execute(
                    "ALTER TABLE batches ADD COLUMN deletion_status TEXT NOT NULL DEFAULT 'active'"
                )
            if "deletion_operation_id" not in columns:
                connection.execute("ALTER TABLE batches ADD COLUMN deletion_operation_id TEXT")
            if "deletion_error" not in columns:
                connection.execute("ALTER TABLE batches ADD COLUMN deletion_error TEXT")

    @contextmanager
    def allow_deleting_writes(self) -> Iterator[None]:
        """Allow a task that was already in flight to finish its bookkeeping."""
        token = _allow_deleting_writes.set(True)
        try:
            yield
        finally:
            _allow_deleting_writes.reset(token)

    @staticmethod
    def _assert_batch_writable(connection: sqlite3.Connection, batch_id: str) -> None:
        if _allow_deleting_writes.get():
            return
        row = connection.execute(
            "SELECT deletion_status FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
        if row is not None and row[0] != "active":
            raise BatchDeletingError("批次正在删除，不能再修改")

    @classmethod
    def _assert_paper_writable(cls, connection: sqlite3.Connection, paper_id: str) -> None:
        if _allow_deleting_writes.get():
            return
        row = connection.execute(
            """SELECT b.deletion_status
            FROM papers p JOIN batches b ON b.id=p.batch_id WHERE p.id=?""",
            (paper_id,),
        ).fetchone()
        if row is not None and row[0] != "active":
            raise BatchDeletingError("批次正在删除，不能再修改")

    def assert_batch_writable(self, batch_id: str) -> None:
        with self.connect() as connection:
            self._assert_batch_writable(connection, batch_id)

    def assert_paper_writable(self, paper_id: str) -> None:
        with self.connect() as connection:
            self._assert_paper_writable(connection, paper_id)

    def is_batch_deleting(self, batch_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT deletion_status FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
        return bool(row and row[0] != "active")

    def is_batch_deleting_for_paper(self, paper_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT b.deletion_status FROM papers p
                JOIN batches b ON b.id=p.batch_id WHERE p.id=?""",
                (paper_id,),
            ).fetchone()
        return bool(row and row[0] != "active")

    def create_batch(
        self,
        *,
        name: str,
        target_library: str,
        library_mode: str,
        items: Iterable[dict[str, Any]],
        reference_manager: str = "zotero",
    ) -> str:
        batch_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self.connect() as connection:
            connection.execute(
                "INSERT INTO batches(id,name,target_library,library_mode,reference_manager,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (batch_id, name, target_library, library_mode, reference_manager, now, now),
            )
            for position, item in enumerate(items, start=1):
                connection.execute(
                    """INSERT INTO papers(
                    id,batch_id,position,input_text,input_doi,input_title,input_year,input_author,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        batch_id,
                        position,
                        item["input_text"],
                        item.get("doi"),
                        item.get("title"),
                        item.get("year"),
                        item.get("author"),
                        now,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO events(batch_id,level,message,created_at) VALUES(?,?,?,?)",
                (batch_id, "info", f"已创建批次，共 {position if 'position' in locals() else 0} 篇", now),
            )
        return batch_id

    def list_batches(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT b.*,
                    COUNT(p.id) AS total,
                    SUM(CASE WHEN p.status='complete' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN p.needs_action IS NOT NULL THEN 1 ELSE 0 END) AS needs_action_count
                FROM batches b LEFT JOIN papers p ON p.batch_id=b.id
                GROUP BY b.id ORDER BY b.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            batch = _row(connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone())
            if batch is None:
                return None
            papers = connection.execute(
                "SELECT * FROM papers WHERE batch_id=? ORDER BY position", (batch_id,)
            ).fetchall()
            batch["papers"] = [self._decode_paper(dict(item)) for item in papers]
            return batch

    def get_paper(self, paper_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            paper = _row(connection.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())
        return self._decode_paper(paper) if paper else None

    @staticmethod
    def _decode_paper(paper: dict[str, Any]) -> dict[str, Any]:
        for field, default in (("authors_json", []), ("metadata_json", {})):
            try:
                paper[field.removesuffix("_json")] = json.loads(paper.get(field) or json.dumps(default))
            except json.JSONDecodeError:
                paper[field.removesuffix("_json")] = default
        paper.pop("authors_json", None)
        paper.pop("metadata_json", None)
        return paper

    def get_candidates(self, paper_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM candidates WHERE paper_id=? ORDER BY rank", (paper_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            result.append(item)
        return result

    def replace_candidates(self, paper_id: str, candidates: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self._lock, self.connect() as connection:
            self._assert_paper_writable(connection, paper_id)
            connection.execute("DELETE FROM candidates WHERE paper_id=?", (paper_id,))
            for rank, candidate in enumerate(candidates, start=1):
                connection.execute(
                    "INSERT INTO candidates(id,paper_id,rank,score,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), paper_id, rank, candidate.get("score"),
                        json.dumps(candidate, ensure_ascii=False), now,
                    ),
                )

    def update_paper(self, paper_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "input_text", "input_doi", "input_title", "input_year", "input_author",
            "doi", "title", "year", "authors_json", "journal", "metadata_json",
            "metadata_status", "pdf_status", "endnote_status", "status", "version",
            "source_url", "pdf_path", "pdf_sha256", "record_number", "needs_action", "error",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported paper fields: {sorted(unknown)}")
        for key in ("authors_json", "metadata_json"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        fields["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in fields)
        with self._lock, self.connect() as connection:
            self._assert_paper_writable(connection, paper_id)
            connection.execute(
                f"UPDATE papers SET {assignments} WHERE id=?",
                (*fields.values(), paper_id),
            )

    def update_batch(self, batch_id: str, **fields: Any) -> None:
        allowed = {
            "status", "paused", "backup_path", "endnote_export_path", "error",
            "library_mode", "reference_manager",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unsupported batch fields: {sorted(unknown)}")
        fields["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in fields)
        with self._lock, self.connect() as connection:
            self._assert_batch_writable(connection, batch_id)
            connection.execute(f"UPDATE batches SET {assignments} WHERE id=?", (*fields.values(), batch_id))

    def event(self, batch_id: str, message: str, *, level: str = "info", paper_id: str | None = None) -> None:
        with self._lock, self.connect() as connection:
            self._assert_batch_writable(connection, batch_id)
            connection.execute(
                "INSERT INTO events(batch_id,paper_id,level,message,created_at) VALUES(?,?,?,?,?)",
                (batch_id, paper_id, level, message, utc_now()),
            )

    def events_after(self, batch_id: str, event_id: int = 0) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE batch_id=? AND id>? ORDER BY id LIMIT 200", (batch_id, event_id)
            ).fetchall()
        return [dict(row) for row in rows]

    def start_operation(self, paper_id: str, step: str, details: dict[str, Any] | None = None) -> str:
        operation_id = str(uuid.uuid4())
        with self._lock, self.connect() as connection:
            self._assert_paper_writable(connection, paper_id)
            connection.execute(
                "INSERT INTO operations(id,paper_id,step,status,details_json,started_at) VALUES(?,?,?,?,?,?)",
                (operation_id, paper_id, step, "pending", json.dumps(details or {}, ensure_ascii=False), utc_now()),
            )
        return operation_id

    def finish_operation(self, operation_id: str, status: str, details: dict[str, Any] | None = None) -> None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """SELECT p.id FROM operations o
                JOIN papers p ON p.id=o.paper_id WHERE o.id=?""",
                (operation_id,),
            ).fetchone()
            if row is not None:
                self._assert_paper_writable(connection, row[0])
            connection.execute(
                "UPDATE operations SET status=?, details_json=?, finished_at=? WHERE id=?",
                (status, json.dumps(details or {}, ensure_ascii=False), utc_now(), operation_id),
            )

    def reset_paper_for_retry(self, paper_id: str) -> None:
        paper = self.get_paper(paper_id)
        if not paper:
            raise KeyError(paper_id)
        if paper["metadata_status"] == "verified":
            status = "ready"
        else:
            status = "queued"
        self.update_paper(paper_id, status=status, needs_action=None, error=None)

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, utc_now()),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def find_active_deletion_operation(self, batch_ids: list[str]) -> dict[str, Any] | None:
        unique_ids = list(dict.fromkeys(batch_ids))
        if not unique_ids:
            return None
        placeholders = ",".join("?" for _ in unique_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT deletion_operation_id, deletion_status
                FROM batches WHERE id IN ({placeholders})""",
                unique_ids,
            ).fetchall()
            operation_ids = {
                row["deletion_operation_id"]
                for row in rows
                if row["deletion_operation_id"] and row["deletion_status"] != "active"
            }
            if len(rows) != len(unique_ids) or len(operation_ids) != 1:
                return None
            operation_id = next(iter(operation_ids))
            operation = connection.execute(
                "SELECT status FROM deletion_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if not operation or operation["status"] not in {"pending", "running"}:
                return None
        return self.get_deletion_operation(operation_id)

    def create_deletion_operation(self, batches: list[dict[str, Any]]) -> str:
        operation_id = str(uuid.uuid4())
        now = utc_now()
        with self._lock, self.connect() as connection:
            connection.execute(
                """INSERT INTO deletion_operations(id,status,created_at,updated_at)
                VALUES(?,?,?,?)""",
                (operation_id, "pending", now, now),
            )
            for item in batches:
                batch_id = str(item["id"])
                row = connection.execute(
                    """SELECT name,deletion_status,deletion_operation_id
                    FROM batches WHERE id=?""",
                    (batch_id,),
                ).fetchone()
                item_error = item.get("error")
                if row is None:
                    item_status = "missing"
                    name = str(item.get("name") or batch_id[:8])
                elif row["deletion_status"] in {"stopping", "deleting"}:
                    item_status = "failed"
                    name = str(row["name"])
                    item_error = f"批次已由删除操作 {row['deletion_operation_id']} 处理"
                else:
                    item_status = "pending"
                    name = str(row["name"])
                    connection.execute(
                        """UPDATE batches
                        SET deletion_status='stopping', deletion_operation_id=?,
                            deletion_error=NULL, updated_at=?
                        WHERE id=?""",
                        (operation_id, now, batch_id),
                    )
                connection.execute(
                    """INSERT INTO deletion_batches(
                    operation_id,batch_id,name,status,paper_count,estimated_bytes,
                    deleted_bytes,error,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        operation_id,
                        batch_id,
                        name,
                        item_status,
                        int(item.get("paper_count") or 0),
                        int(item.get("bytes") or 0),
                        0,
                        item_error,
                        now,
                        now,
                    ),
                )
        return operation_id

    def get_deletion_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            operation = _row(
                connection.execute(
                    "SELECT * FROM deletion_operations WHERE id=?", (operation_id,)
                ).fetchone()
            )
            if operation is None:
                return None
            items = connection.execute(
                """SELECT batch_id,name,status,paper_count,estimated_bytes,
                deleted_bytes,error,created_at,updated_at
                FROM deletion_batches WHERE operation_id=? ORDER BY created_at,batch_id""",
                (operation_id,),
            ).fetchall()
        operation["batches"] = [dict(item) for item in items]
        return operation

    def list_recoverable_deletions(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id FROM deletion_operations
                WHERE status IN ('pending','running') ORDER BY created_at"""
            ).fetchall()
        return [item for row in rows if (item := self.get_deletion_operation(row["id"]))]

    def set_deletion_operation_status(self, operation_id: str, status: str) -> None:
        now = utc_now()
        finished_at = now if status in {"completed", "partial_failed"} else None
        with self._lock, self.connect() as connection:
            connection.execute(
                """UPDATE deletion_operations
                SET status=?,updated_at=?,finished_at=? WHERE id=?""",
                (status, now, finished_at, operation_id),
            )

    def set_deletion_batch_status(
        self,
        operation_id: str,
        batch_id: str,
        status: str,
        *,
        deleted_bytes: int | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        with self._lock, self.connect() as connection:
            values: list[Any] = [status, error, now]
            deleted_sql = ""
            if deleted_bytes is not None:
                deleted_sql = ",deleted_bytes=?"
                values.append(max(0, int(deleted_bytes)))
            values.extend((operation_id, batch_id))
            connection.execute(
                f"""UPDATE deletion_batches SET status=?,error=?,updated_at=?{deleted_sql}
                WHERE operation_id=? AND batch_id=?""",
                values,
            )
            batch_state = {
                "pending": "stopping",
                "stopping": "stopping",
                "deleting": "deleting",
                "failed": "failed",
            }.get(status)
            if batch_state:
                connection.execute(
                    """UPDATE batches SET deletion_status=?,deletion_operation_id=?,
                    deletion_error=?,updated_at=? WHERE id=?""",
                    (batch_state, operation_id, error, now, batch_id),
                )

    def finalize_deletion_operation(self, operation_id: str) -> str:
        with self.connect() as connection:
            statuses = [
                row[0]
                for row in connection.execute(
                    "SELECT status FROM deletion_batches WHERE operation_id=?", (operation_id,)
                ).fetchall()
            ]
        status = (
            "partial_failed"
            if any(item == "failed" for item in statuses)
            else "completed"
            if all(item in {"completed", "missing"} for item in statuses)
            else "running"
        )
        self.set_deletion_operation_status(operation_id, status)
        return status

    def list_batch_paper_ids(self, batch_id: str) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM papers WHERE batch_id=? ORDER BY position", (batch_id,)
            ).fetchall()
        return [row[0] for row in rows]

    def delete_batch_transaction(
        self, operation_id: str, batch_id: str, *, deleted_bytes: int
    ) -> None:
        """Delete the batch and mark its deletion item complete in one transaction."""
        now = utc_now()
        with self._lock, self.connect() as connection:
            connection.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            connection.execute(
                """UPDATE deletion_batches
                SET status='completed',deleted_bytes=?,error=NULL,updated_at=?
                WHERE operation_id=? AND batch_id=?""",
                (max(0, int(deleted_bytes)), now, operation_id, batch_id),
            )
