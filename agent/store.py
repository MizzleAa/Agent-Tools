"""실행 기록 저장소 (SQLite 파일 하나: data/agent.db).

runs   : 실행 한 건의 요약 (작업, 상태, 토큰, 시간) + 누가(user_id·company_id) 어떤 모델로(provider·connection)
events : 그 실행에서 나온 이벤트 전체 (agent/events.py 의 to_dict() 를 JSON 으로)
→ 웹 UI 의 '기록' 화면은 events 를 다시 읽어 그대로 재생한다.
user_id 가 빈 실행은 로그인 없이('혼자 쓰기') 한 것이다. 로그인 사용을 켜면 첫 관리자에게 넘어간다 (assign_orphans).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.events import Event, Final, RunStarted

SCHEMA = """
create table if not exists runs (
    id text primary key,
    task text not null,
    profile text not null,
    model text not null,
    status text not null default 'running',
    answer text not null default '',
    steps integer not null default 0,
    prompt_tokens integer not null default 0,
    completion_tokens integer not null default 0,
    seconds real not null default 0,
    created_at text not null,
    finished_at text
);
create table if not exists events (
    id integer primary key autoincrement,
    run_id text not null references runs(id),
    seq integer not null,
    type text not null,
    data text not null,
    created_at text not null
);
create index if not exists idx_events_run on events(run_id, seq);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # 웹 서버에서는 실행 스레드와 요청 스레드가 같은 연결을 쓰므로 잠금으로 직렬화한다.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("pragma journal_mode=wal")
            self.db.executescript(SCHEMA)
            columns = {r["name"] for r in self.db.execute("pragma table_info(runs)")}
            if "parent_id" not in columns:   # 예전 DB 에 '이어서 하기' 칸 추가
                self.db.execute("alter table runs add column parent_id text not null default ''")
            if "workdir" not in columns:     # 예전 DB 에 '세션 폴더' 칸 추가
                self.db.execute("alter table runs add column workdir text not null default ''")
            for col in ("user_id", "company_id", "provider", "connection"):   # 로그인 사용 · 외부 모델 칸
                if col not in columns:
                    self.db.execute(f"alter table runs add column {col} text not null default ''")
            self.db.execute("create index if not exists idx_runs_user on runs(user_id, created_at)")

    def record(self, run_id: str, seq: int, ev: Event) -> None:
        data = ev.to_dict()
        with self.lock, self.db:
            if isinstance(ev, RunStarted):
                self.db.execute(
                    "insert into runs(id, task, profile, model, created_at, parent_id, workdir) values (?,?,?,?,?,?,?)",
                    (ev.run_id, ev.task, ev.profile, ev.model, _now(), ev.parent_run_id, ev.workdir))
            self.db.execute("insert into events(run_id, seq, type, data, created_at) values (?,?,?,?,?)",
                            (run_id, seq, data["type"], json.dumps(data, ensure_ascii=False), _now()))
            if isinstance(ev, Final):
                self.db.execute(
                    "update runs set status=?, answer=?, steps=?, prompt_tokens=?, completion_tokens=?, seconds=?,"
                    " finished_at=? where id=?",
                    (ev.status, ev.answer, ev.steps, ev.prompt_tokens, ev.completion_tokens, ev.seconds, _now(), run_id))

    def mark_interrupted(self) -> int:
        """서버가 꺼지는 바람에 끝나지 못한 실행을 'interrupted' 로 표시한다 (서버 시작 시 호출)."""
        with self.lock, self.db:
            return self.db.execute("update runs set status='interrupted', finished_at=? where status='running'",
                                   (_now(),)).rowcount

    def thread_ids(self, root_id: str) -> list[str]:
        """root 실행과 그 실행에서 '이어서' 시작된 모든 실행의 id (대화 하나 전체)."""
        sql = ("with recursive chain(id) as (select id from runs where id = ? "
               "union select r.id from runs r join chain c on r.parent_id = c.id) select id from chain")
        with self.lock:
            return [r["id"] for r in self.db.execute(sql, (root_id,))]

    def delete_runs(self, ids: list[str]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.lock, self.db:
            self.db.execute(f"delete from events where run_id in ({marks})", ids)
            return self.db.execute(f"delete from runs where id in ({marks})", ids).rowcount

    def tag(self, run_id: str, **cols: str) -> None:
        """실행에 누가·어떤 모델로 했는지 적는다 (RunStarted 를 기록한 바로 뒤에 부른다)."""
        allowed = {k: v for k, v in cols.items() if k in ("user_id", "company_id", "provider", "connection")}
        if allowed:
            with self.lock, self.db:
                self.db.execute(f"update runs set {', '.join(f'{k}=?' for k in allowed)} where id=?",
                                (*allowed.values(), run_id))

    def list_runs(self, limit: int = 50, user_id: str | None = None) -> list[dict[str, Any]]:
        """최근 실행. user_id 를 주면 그 사람 것만 (None = 전부)."""
        where, args = ("where user_id=? ", (user_id,)) if user_id is not None else ("", ())
        with self.lock:
            rows = self.db.execute(f"select * from runs {where}order by created_at desc, rowid desc limit ?", (*args, limit))
            return [dict(r) for r in rows]

    def owner(self, run_id: str) -> str | None:
        """실행한 사람 id ('' = 혼자 쓰기). 없는 실행이면 None."""
        with self.lock:
            row = self.db.execute("select user_id from runs where id=?", (run_id,)).fetchone()
        return None if row is None else row["user_id"]

    def assign_orphans(self, user_id: str, company_id: str = "") -> int:
        """로그인 없이 한 실행(user_id 가 빈 것)을 이 사람 것으로 넘긴다."""
        with self.lock, self.db:
            return self.db.execute("update runs set user_id=?, company_id=? where user_id=''", (user_id, company_id)).rowcount

    _FILTERS = {"user_id": "user_id = ?", "company_id": "company_id = ?", "provider": "provider = ?",
                "connection": "connection = ?", "model": "model = ?", "since": "created_at >= ?", "until": "created_at < ?",
                "status": "status = ?"}

    def _where(self, filters: dict[str, str | None]) -> tuple[str, list[str]]:
        parts, args = [], []
        for key, value in filters.items():
            if value is None or value == "" and key not in ("user_id", "provider"):
                continue
            parts.append(self._FILTERS[key])
            args.append(value)
        return (" where " + " and ".join(parts)) if parts else "", args

    def search(self, limit: int = 200, offset: int = 0, **filters: str | None) -> tuple[int, list[dict[str, Any]]]:
        """관리자 '기록': 조건에 맞는 실행 (최근 순)과 전체 개수."""
        where, args = self._where(filters)
        with self.lock:
            total = self.db.execute(f"select count(*) from runs{where}", args).fetchone()[0]
            rows = self.db.execute(f"select * from runs{where} order by created_at desc, rowid desc limit ? offset ?",
                                   (*args, limit, offset))
            return total, [dict(r) for r in rows]

    def usage(self, group: str, **filters: str | None) -> list[dict[str, Any]]:
        """사용량 합계: group = user_id · company_id · connection · model. 실행 수 · 토큰 · 시간."""
        if group not in ("user_id", "company_id", "connection", "model", "provider"):
            raise ValueError("group 은 user_id · company_id · connection · model · provider 중 하나입니다")
        where, args = self._where(filters)
        sql = (f"select {group} as key, count(*) as runs, sum(prompt_tokens) as prompt_tokens, "
               f"sum(completion_tokens) as completion_tokens, sum(seconds) as seconds, max(created_at) as last_at "
               f"from runs{where} group by {group} order by runs desc")
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("select * from runs where id=?", (run_id,)).fetchone()
            if row is None:
                return None
            events = self.db.execute("select data from events where run_id=? order by seq", (run_id,))
            return {**dict(row), "events": [json.loads(e["data"]) for e in events]}
