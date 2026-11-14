"""CLI 대화의 SQLite (data/claude_code/claude_code.db).

여기에는 프로젝트·세션 메타데이터·설정·프로세스 기록을 둔다.
Claude Code 대화 내용은 저장하지 않는다 — 원본은 언제나 Claude Code 의 세션 기록이다.
Codex · Gemini 엔진은 화면에 보일 기록(정규화한 메시지)을 engine_messages 에 남긴다 — 그 CLI 의 기록은 모델 문맥 복원용으로만 쓴다.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    path            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_used_at    TEXT,
    last_session_id TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    claude_session_id TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    status            TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    model             TEXT,
    permission_mode   TEXT NOT NULL DEFAULT 'default',
    has_transcript    INTEGER NOT NULL DEFAULT 0,
    origin            TEXT NOT NULL DEFAULT 'gui',
    last_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_used_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_project ON sessions(project_id, last_used_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS process_registry (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    process_id       INTEGER NOT NULL,
    status           TEXT NOT NULL,
    cli_path         TEXT,
    cli_version      TEXT,
    exit_code        INTEGER,
    proc_create_time REAL,
    started_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_proc_session ON process_registry(session_id, id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_messages (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    data       TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
            # 나중에 생긴 칸: 용도(설정 · 프롬프트의 프로필 — CLI 에 붙는 지침) · 추론 강도 · 하네스(회사·프로젝트) · 엔진
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
            for col in ("profile", "effort", "harness"):
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
            if "engine" not in cols:   # claude · codex · gemini (예전 세션은 모두 Claude Code)
                self._conn.execute("ALTER TABLE sessions ADD COLUMN engine TEXT NOT NULL DEFAULT 'claude'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 저수준 -------------------------------------------------------------
    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def _update(self, table: str, key: str, key_value: Any, fields: dict[str, Any]) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE {table} SET {cols} WHERE {key} = ?", (*fields.values(), key_value))

    # ---- 프로젝트 -----------------------------------------------------------
    def list_projects(self) -> list[dict[str, Any]]:
        return self.query(
            "SELECT p.*, (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id) AS session_count "
            "FROM projects p ORDER BY COALESCE(p.last_used_at, p.created_at) DESC"
        )

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        return self.one(
            "SELECT p.*, (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id) AS session_count "
            "FROM projects p WHERE p.id = ?",
            (project_id,),
        )

    def find_project_by_path(self, path: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM projects WHERE lower(path) = lower(?)", (path,))

    def create_project(self, name: str, path: str) -> dict[str, Any]:
        pid, ts = new_id(), now_iso()
        self.execute(
            "INSERT INTO projects(id, name, path, created_at, updated_at, last_used_at) VALUES (?,?,?,?,?,?)",
            (pid, name, path, ts, ts, ts),
        )
        return self.get_project(pid)  # type: ignore[return-value]

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any] | None:
        fields["updated_at"] = now_iso()
        self._update("projects", "id", project_id, fields)
        return self.get_project(project_id)

    def touch_project(self, project_id: str, session_id: str | None = None) -> None:
        f: dict[str, Any] = {"last_used_at": now_iso()}
        if session_id:
            f["last_session_id"] = session_id
        self._update("projects", "id", project_id, f)

    def delete_project(self, project_id: str) -> None:
        self.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # ---- 세션 ---------------------------------------------------------------
    def list_sessions(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id:
            return self.query(
                "SELECT * FROM sessions WHERE project_id = ? ORDER BY COALESCE(last_used_at, created_at) DESC",
                (project_id,),
            )
        return self.query("SELECT * FROM sessions ORDER BY COALESCE(last_used_at, created_at) DESC")

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM sessions WHERE id = ?", (session_id,))

    def find_session_by_claude_id(self, claude_session_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM sessions WHERE claude_session_id = ?", (claude_session_id,))

    def create_session(
        self,
        project_id: str,
        claude_session_id: str,
        title: str,
        working_directory: str,
        status: str,
        model: str | None = None,
        permission_mode: str = "default",
        has_transcript: bool = False,
        origin: str = "gui",
        profile: str | None = None,
        effort: str | None = None,
        harness: str | None = None,
        engine: str = "claude",
    ) -> dict[str, Any]:
        sid, ts = new_id(), now_iso()
        self.execute(
            "INSERT INTO sessions(id, project_id, claude_session_id, title, status, working_directory, model, "
            "permission_mode, has_transcript, origin, created_at, updated_at, last_used_at, profile, effort, harness, engine) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, project_id, claude_session_id, title, status, working_directory, model, permission_mode,
             int(has_transcript), origin, ts, ts, ts, profile, effort, harness, engine),
        )
        return self.get_session(sid)  # type: ignore[return-value]

    def update_session(self, session_id: str, **fields: Any) -> dict[str, Any] | None:
        if "has_transcript" in fields:
            fields["has_transcript"] = int(bool(fields["has_transcript"]))
        fields["updated_at"] = now_iso()
        self._update("sessions", "id", session_id, fields)
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> None:
        self.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.execute("DELETE FROM engine_messages WHERE session_id = ?", (session_id,))
        self.execute("UPDATE projects SET last_session_id = NULL WHERE last_session_id = ?", (session_id,))

    def last_used_session(self) -> dict[str, Any] | None:
        return self.one("SELECT * FROM sessions ORDER BY COALESCE(last_used_at, created_at) DESC LIMIT 1")

    # ---- Codex · Gemini 대화 기록 (화면용) ---------------------------------
    def add_engine_message(self, session_id: str, message: dict[str, Any]) -> None:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM engine_messages WHERE session_id = ?",
                                     (session_id,)).fetchone()
            self._conn.execute("INSERT INTO engine_messages(session_id, seq, data) VALUES (?,?,?)",
                               (session_id, row[0], json.dumps(message, ensure_ascii=False)))

    def engine_messages(self, session_id: str, limit: int, before: int | None) -> dict[str, Any]:
        total = (self.one("SELECT COUNT(*) AS n FROM engine_messages WHERE session_id = ?", (session_id,)) or {"n": 0})["n"]
        end = total if before is None else max(0, min(before, total))
        start = max(0, end - limit)
        rows = self.query("SELECT data FROM engine_messages WHERE session_id = ? ORDER BY seq LIMIT ? OFFSET ?",
                          (session_id, end - start, start))
        return {"total": total, "start": start, "end": end, "messages": [json.loads(r["data"]) for r in rows]}

    # ---- 설정 ---------------------------------------------------------------
    def get_settings(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in self.query("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
        return out

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ---- 프로세스 기록 ------------------------------------------------------
    def add_process(self, session_id: str, pid: int, status: str, cli_path: str | None,
                    cli_version: str | None, create_time: float | None) -> int:
        ts = now_iso()
        return self.execute(
            "INSERT INTO process_registry(session_id, process_id, status, cli_path, cli_version, proc_create_time, "
            "started_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, pid, status, cli_path, cli_version, create_time, ts, ts),
        )

    def update_process(self, row_id: int, **fields: Any) -> None:
        fields["updated_at"] = now_iso()
        self._update("process_registry", "id", row_id, fields)

    def get_process(self, row_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM process_registry WHERE id = ?", (row_id,))

    def live_process_rows(self) -> list[dict[str, Any]]:
        """종료 기록이 없는(살아 있다고 기록된) 프로세스."""
        return self.query(
            "SELECT * FROM process_registry WHERE status IN ('STARTING','READY','BUSY','STOPPING','ORPHANED') ORDER BY id"
        )

    def recent_processes(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM process_registry ORDER BY id DESC LIMIT ?", (limit,))

    def prune_processes(self, keep: int = 500) -> None:
        self.execute(
            "DELETE FROM process_registry WHERE id NOT IN (SELECT id FROM process_registry ORDER BY id DESC LIMIT ?) "
            "AND status NOT IN ('STARTING','READY','BUSY','STOPPING','ORPHANED')",
            (keep,),
        )
