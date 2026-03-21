from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from loki2.store.models import Issue

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    identifier TEXT NOT NULL,
    title TEXT DEFAULT '',
    phase TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    parent_id TEXT,
    parent_identifier TEXT,
    repo_path TEXT,
    base_branch TEXT,
    branch TEXT,
    session_id TEXT,
    worktree_path TEXT,
    pid INTEGER,
    retry_count INTEGER DEFAULT 0,
    error TEXT,
    metadata TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    processed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    model TEXT DEFAULT '',
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    duration_s REAL,
    cost_usd REAL,
    turns INTEGER,
    status TEXT DEFAULT 'running',
    error TEXT,
    log_file TEXT
);
"""


class Database:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None

    def connect(self):
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self):
        if self._db:
            self._db.close()

    def upsert_issue(self, issue: Issue):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute(
                """INSERT INTO issues (id, identifier, title, phase, status, parent_id,
                   parent_identifier, repo_path, base_branch, branch, session_id,
                   worktree_path, pid, retry_count, error, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                   title=excluded.title, phase=excluded.phase, status=excluded.status,
                   parent_id=excluded.parent_id, parent_identifier=excluded.parent_identifier,
                   repo_path=excluded.repo_path, base_branch=excluded.base_branch,
                   branch=excluded.branch, session_id=excluded.session_id,
                   worktree_path=excluded.worktree_path, pid=excluded.pid,
                   retry_count=excluded.retry_count, error=excluded.error,
                   metadata=excluded.metadata, updated_at=?""",
                (issue.id, issue.identifier, issue.title, issue.phase, issue.status,
                 issue.parent_id, issue.parent_identifier, issue.repo_path,
                 issue.base_branch, issue.branch, issue.session_id,
                 issue.worktree_path, issue.pid, issue.retry_count, issue.error,
                 json.dumps(issue.metadata), issue.created_at or now, now, now),
            )
            self._db.commit()

    def get_issue(self, issue_id: str) -> Issue | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if not row:
            return None
        return self._row_to_issue(row)

    def get_issues_by_status(self, status: str) -> list[Issue]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM issues WHERE status = ? ORDER BY created_at", (status,)).fetchall()
        return [self._row_to_issue(r) for r in rows]

    def get_running_issues(self) -> list[Issue]:
        return self.get_issues_by_status("running")

    def update_status(self, issue_id: str, status: str, **kwargs):
        now = datetime.now(timezone.utc).isoformat()
        sets = ["status = ?", "updated_at = ?"]
        vals: list = [status, now]
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(issue_id)
        with self._lock:
            self._db.execute(f"UPDATE issues SET {', '.join(sets)} WHERE id = ?", vals)
            self._db.commit()

    def log_event(self, issue_id: str, event_type: str, payload: dict | None = None):
        with self._lock:
            self._db.execute(
                "INSERT INTO events (issue_id, event_type, payload) VALUES (?, ?, ?)",
                (issue_id, event_type, json.dumps(payload or {})),
            )
            self._db.commit()

    def start_execution(self, issue_id: str, phase: str,
                        model: str = "", log_file: str = "") -> int:
        with self._lock:
            cursor = self._db.execute(
                """INSERT INTO executions (issue_id, phase, model, log_file)
                   VALUES (?, ?, ?, ?)""",
                (issue_id, phase, model, log_file),
            )
            self._db.commit()
            return cursor.lastrowid

    def finish_execution(self, exec_id: int, *, status: str = "done",
                         duration_s: float = 0, cost_usd: float = 0,
                         turns: int = 0, error: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._db.execute(
                """UPDATE executions SET finished_at=?, duration_s=?, cost_usd=?,
                   turns=?, status=?, error=? WHERE id=?""",
                (now, duration_s, cost_usd, turns, status, error, exec_id),
            )
            self._db.commit()

    def _row_to_issue(self, row) -> Issue:
        return Issue(
            id=row["id"],
            identifier=row["identifier"],
            title=row["title"] or "",
            phase=row["phase"] or "",
            status=row["status"],
            parent_id=row["parent_id"],
            parent_identifier=row["parent_identifier"],
            repo_path=row["repo_path"],
            base_branch=row["base_branch"],
            branch=row["branch"],
            session_id=row["session_id"],
            worktree_path=row["worktree_path"],
            pid=row["pid"],
            retry_count=row["retry_count"] or 0,
            error=row["error"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
