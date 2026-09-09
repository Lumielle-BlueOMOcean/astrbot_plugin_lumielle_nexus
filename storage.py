"""SQLite persistence for Lumielle Nexus."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class Storage:
    """Small, synchronous SQLite store used behind TaskManager's async lock."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "lumielle_nexus.db"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS group_bindings (
                    alias TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (platform_id, alias),
                    UNIQUE (platform_id, group_id)
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    group_alias TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    creator_private_origin TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    run_at TEXT,
                    payload TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '{}',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    parent_id TEXT,
                    occurrence_key TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_due
                    ON tasks(status, run_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_group
                    ON tasks(platform_id, group_id, status);

                CREATE TABLE IF NOT EXISTS collection_entries (
                    task_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    raw_message TEXT NOT NULL,
                    parsed_data TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, sender_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                );
                """,
            )
            columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "parent_id" not in columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN parent_id TEXT")
            if "occurrence_key" not in columns:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN occurrence_key TEXT")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)")
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_parent_occurrence
                ON tasks(parent_id, occurrence_key)
                WHERE parent_id IS NOT NULL AND occurrence_key IS NOT NULL
                """,
            )
            self._conn.commit()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def next_task_id(self, prefix: str, date_token: str) -> str:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM tasks WHERE id LIKE ?",
                (f"{prefix}-{date_token}-%",),
            ).fetchall()
        highest = 0
        for row in rows:
            try:
                highest = max(highest, int(str(row["id"]).rsplit("-", 1)[1]))
            except (IndexError, ValueError):
                continue
        return f"{prefix}-{date_token}-{highest + 1:03d}"

    def upsert_binding(
        self,
        alias: str,
        group_id: str,
        platform_id: str,
        created_by: str,
        created_at: str,
    ) -> dict[str, Any]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                alias_row = self._conn.execute(
                    "SELECT * FROM group_bindings WHERE platform_id = ? AND alias = ?",
                    (platform_id, alias),
                ).fetchone()
                if alias_row and alias_row["group_id"] != group_id:
                    raise ValueError(f"群别名已绑定到其他群：{alias}")
                group_row = self._conn.execute(
                    "SELECT * FROM group_bindings WHERE platform_id = ? AND group_id = ?",
                    (platform_id, group_id),
                ).fetchone()
                if group_row:
                    self._conn.execute(
                        """
                        UPDATE group_bindings
                        SET alias = ?, created_by = ?, created_at = ?
                        WHERE platform_id = ? AND group_id = ?
                        """,
                        (alias, created_by, created_at, platform_id, group_id),
                    )
                else:
                    self._conn.execute(
                        """
                        INSERT INTO group_bindings
                            (alias, group_id, platform_id, created_by, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (alias, group_id, platform_id, created_by, created_at),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_binding(alias, platform_id)

    def get_binding(self, alias_or_group: str, platform_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM group_bindings
                WHERE platform_id = ? AND (alias = ? OR group_id = ?)
                ORDER BY CASE WHEN alias = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (platform_id, alias_or_group, alias_or_group, alias_or_group),
            ).fetchone()
        return self._row(row)

    def list_bindings(self, platform_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if platform_id:
                rows = self._conn.execute(
                    "SELECT * FROM group_bindings WHERE platform_id = ? ORDER BY alias",
                    (platform_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM group_bindings ORDER BY platform_id, alias",
                ).fetchall()
        return self._rows(rows)

    def create_task(
        self,
        task_id: str,
        task_type: str,
        status: str,
        group_id: str,
        group_alias: str,
        platform_id: str,
        creator_id: str,
        creator_private_origin: str,
        created_at: str,
        run_at: str | None,
        payload: dict[str, Any],
        parent_id: str | None = None,
        occurrence_key: str | None = None,
    ) -> dict[str, Any]:
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tasks
                    (id, type, status, group_id, group_alias, platform_id,
                     creator_id, creator_private_origin, created_at, run_at,
                     payload, updated_at, parent_id, occurrence_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task_type,
                    status,
                    group_id,
                    group_alias,
                    platform_id,
                    creator_id,
                    creator_private_origin,
                    created_at,
                    run_at,
                    payload_json,
                    created_at,
                    parent_id,
                    occurrence_key,
                ),
            )
            self._conn.commit()
        return self.get_task(task_id)

    def create_collection_task(
        self,
        task_id: str,
        group_id: str,
        group_alias: str,
        platform_id: str,
        creator_id: str,
        creator_private_origin: str,
        created_at: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                active = self._conn.execute(
                    """
                    SELECT id FROM tasks
                    WHERE type = 'COLLECTION' AND status = 'ACTIVE'
                      AND platform_id = ? AND group_id = ?
                    LIMIT 1
                    """,
                    (platform_id, group_id),
                ).fetchone()
                if active:
                    raise ValueError(f"该群已有进行中的收集任务：{active['id']}")
                self._conn.execute(
                    """
                    INSERT INTO tasks
                        (id, type, status, group_id, group_alias, platform_id,
                         creator_id, creator_private_origin, created_at, run_at,
                         payload, updated_at)
                    VALUES (?, 'COLLECTION', 'ACTIVE', ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        task_id,
                        group_id,
                        group_alias,
                        platform_id,
                        creator_id,
                        creator_private_origin,
                        created_at,
                        payload_json,
                        created_at,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._row(row)

    def list_tasks(
        self,
        platform_id: str | None = None,
        group_id: str | None = None,
        include_children: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: list[str] = []
        clauses: list[str] = [] if include_children else ["parent_id IS NULL"]
        if platform_id:
            clauses.append("platform_id = ?")
            params.append(platform_id)
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return self._rows(rows)

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        run_at: str | None = None,
        retry_count: int | None = None,
        last_error: str | None = None,
        updated_at: str,
        finished_at: str | None = None,
    ) -> dict[str, Any]:
        assignments = ["updated_at = ?"]
        params: list[Any] = [updated_at]
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
        if result is not None:
            assignments.append("result = ?")
            params.append(json.dumps(result, ensure_ascii=False))
        if run_at is not None:
            assignments.append("run_at = ?")
            params.append(run_at)
        if retry_count is not None:
            assignments.append("retry_count = ?")
            params.append(retry_count)
        if last_error is not None:
            assignments.append("last_error = ?")
            params.append(last_error)
        if finished_at is not None:
            assignments.append("finished_at = ?")
            params.append(finished_at)
        params.append(task_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            self._conn.commit()
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"任务不存在：{task_id}")
        return task

    def cancel_task(self, task_id: str, platform_id: str, updated_at: str) -> dict[str, Any]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                task = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ? AND platform_id = ?",
                    (task_id, platform_id),
                ).fetchone()
                if task is None:
                    raise KeyError(f"任务不存在：{task_id}")
                if task["status"] in {"PENDING", "ACTIVE"}:
                    self._conn.execute(
                        "UPDATE tasks SET status = 'CANCELLED', updated_at = ?, finished_at = ? WHERE id = ?",
                        (updated_at, updated_at, task_id),
                    )
                    self._conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'CANCELLED', updated_at = ?, finished_at = ?
                        WHERE parent_id = ? AND status = 'PENDING'
                        """,
                        (updated_at, updated_at, task_id),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_task(task_id)

    def claim_due_tasks(self, now_iso: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                rows = self._conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE type = 'REMINDER' AND status = 'PENDING'
                      AND run_at IS NOT NULL AND run_at <= ?
                    ORDER BY run_at, created_at
                    LIMIT ?
                    """,
                    (now_iso, limit),
                ).fetchall()
                for row in rows:
                    self._conn.execute(
                        "UPDATE tasks SET status = 'PROCESSING', updated_at = ? WHERE id = ?",
                        (now_iso, row["id"]),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return [self.get_task(row["id"]) for row in rows]

    def recover_processing_reminders(self, updated_at: str) -> int:
        """Return reminders left mid-send to the durable pending queue."""
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE tasks
                SET status = 'PENDING', updated_at = ?, last_error = COALESCE(last_error, '')
                WHERE type = 'REMINDER' AND status = 'PROCESSING'
                """,
                (updated_at,),
            )
            self._conn.commit()
        return int(cursor.rowcount)

    def list_schedule_parents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE type IN ('DDL', 'RECURRING', 'COURSE') AND status = 'ACTIVE'
                ORDER BY run_at, created_at
                """,
            ).fetchall()
        return self._rows(rows)

    def list_children(self, parent_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE parent_id = ? ORDER BY run_at, created_at",
                (parent_id,),
            ).fetchall()
        return self._rows(rows)

    def create_child_reminder(
        self,
        task_id: str,
        parent_id: str,
        occurrence_key: str,
        group_id: str,
        group_alias: str,
        platform_id: str,
        creator_id: str,
        creator_private_origin: str,
        created_at: str,
        run_at: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO tasks
                    (id, type, status, group_id, group_alias, platform_id,
                     creator_id, creator_private_origin, created_at, run_at,
                     payload, updated_at, parent_id, occurrence_key)
                VALUES (?, 'REMINDER', 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    group_id,
                    group_alias,
                    platform_id,
                    creator_id,
                    creator_private_origin,
                    created_at,
                    run_at,
                    payload_json,
                    created_at,
                    parent_id,
                    occurrence_key,
                ),
            )
            self._conn.commit()
        task = self.get_task(task_id)
        if task is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE parent_id = ? AND occurrence_key = ?",
                    (parent_id, occurrence_key),
                ).fetchone()
            task = self._row(row)
        if task is None:
            raise KeyError(f"无法创建提醒子任务：{task_id}")
        return task

    def get_active_collection(self, platform_id: str, group_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE type = 'COLLECTION' AND status = 'ACTIVE'
                  AND platform_id = ? AND group_id = ?
                LIMIT 1
                """,
                (platform_id, group_id),
            ).fetchone()
        return self._row(row)

    def transition_collection_to_processing(
        self,
        task_id: str,
        platform_id: str,
        updated_at: str,
    ) -> dict[str, Any]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                task = self._conn.execute(
                    "SELECT * FROM tasks WHERE id = ? AND platform_id = ?",
                    (task_id, platform_id),
                ).fetchone()
                if task is None:
                    raise KeyError(f"任务不存在：{task_id}")
                if task["type"] != "COLLECTION" or task["status"] != "ACTIVE":
                    raise ValueError(f"任务当前不能结束：{task['status']}")
                self._conn.execute(
                    "UPDATE tasks SET status = 'PROCESSING', updated_at = ? WHERE id = ?",
                    (updated_at, task_id),
                )
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'CANCELLED', updated_at = ?, finished_at = ?
                    WHERE parent_id = ? AND status = 'PENDING'
                    """,
                    (updated_at, updated_at, task_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_task(task_id)

    def upsert_entry(
        self,
        task_id: str,
        sender_id: str,
        sender_name: str,
        raw_message: str,
        parsed_data: dict[str, str],
        submitted_at: str,
    ) -> dict[str, Any]:
        parsed_json = json.dumps(parsed_data, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO collection_entries
                    (task_id, sender_id, sender_name, raw_message, parsed_data,
                     submitted_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, sender_id) DO UPDATE SET
                    sender_name = excluded.sender_name,
                    raw_message = excluded.raw_message,
                    parsed_data = excluded.parsed_data,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    sender_id,
                    sender_name,
                    raw_message,
                    parsed_json,
                    submitted_at,
                    submitted_at,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM collection_entries WHERE task_id = ? AND sender_id = ?",
                (task_id, sender_id),
            ).fetchone()
        return self._row(row)

    def get_entry(self, task_id: str, sender_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM collection_entries WHERE task_id = ? AND sender_id = ?",
                (task_id, sender_id),
            ).fetchone()
        return self._row(row)

    def list_entries(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM collection_entries WHERE task_id = ? ORDER BY sender_id",
                (task_id,),
            ).fetchall()
        return self._rows(rows)
