"""관리 서버가 함께 쓰는 상태: 설정 읽기, 실행 기록 DB, 계정 DB, 실행 중인 작업, 로컬 모델 차례, 모델 서버, 보안 토큰.

라우터(server/api/*)는 이 모듈의 속성을 **부를 때마다** 읽는다 (`deps.store`, `deps.load_settings()` …).
그래서 테스트는 `monkeypatch.setattr(deps, "store", …)` 처럼 여기 하나만 바꿔 끼우면 된다.
"""

from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from fastapi import HTTPException, Request

from agent import accounts as accounts_mod
from agent import config, loop, workflow
from agent.accounts import User
from agent.events import ApprovalRequested
from agent.runtime import LlamaServer
from agent.store import Store
from agent.tools import REGISTRY

# 상태를 바꾸는 요청(GET 이 아닌 것)은 이 토큰이 X-Agent-Token 헤더에 있어야 한다. 화면(index.html)에 넣어 준다.
# 개발 중 Vite 개발 서버(web/)를 쓸 때는 AGENT_TOKEN 환경변수로 같은 값을 양쪽에 준다.
TOKEN = os.environ.get("AGENT_TOKEN") or secrets.token_urlsafe(24)
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}   # 이 PC. 다른 PC 가 들어올 주소는 config/access.yaml 의 public_hosts (관리 › 접속 설정)

AutoMode = Literal["none", "writes", "all"]

load_settings = config.load_settings      # 테스트가 임시 설정으로 바꿔 끼운다
Agent = loop.Agent                        # 테스트가 가짜 모델을 쓰는 Agent 로 바꿔 끼운다
WorkflowRunner = workflow.WorkflowRunner

settings = load_settings()
store = Store(settings.data_path / "agent.db")
store.mark_interrupted()
accounts = accounts_mod.AccountStore(settings.data_path / "accounts.db")
llm_server = LlamaServer(settings)


def allowed_hosts() -> set[str]:
    return ALLOWED_HOSTS | {h.strip().lower() for h in accounts_mod.load_access().public_hosts if h.strip()}


# ───────────────────────── 누가 요청했나 (server/app.py 의 guard 가 정한다)

def user(request: Request | None = None) -> User | None:
    """로그인한 사람. None = 로그인 없이 쓰는 '혼자 쓰기' (이 PC 사람 = 관리자)."""
    if request is not None:
        return getattr(request.state, "user", None)
    return accounts_mod.current()


def require_admin(request: Request) -> None:
    """관리자만 (라우터에 dependencies 로 붙인다). 혼자 쓰기에서는 늘 통과."""
    if not accounts_mod.is_admin(user(request)):
        raise HTTPException(403, "관리자만 할 수 있습니다")


def require_admin_now() -> None:
    """핸들러 안에서 부르는 관리자 확인 (요청 객체 없이 — 미들웨어가 정한 지금 사용자 기준)."""
    if not accounts_mod.is_admin(accounts_mod.current()):
        raise HTTPException(403, "관리자만 할 수 있습니다")


def settings_for(who: User | None, s: config.Settings | None = None) -> config.Settings:
    """그 사람의 실행에 쓸 설정. 로그인 사용 모드에서는 작업 폴더가 workspace/users/<아이디> 로 바뀌고,
    허용 폴더 중 작업 폴더(와 그 안쪽)는 그 사람 폴더로만 남는다 → 남의 작업 폴더를 읽거나 쓸 수 없다."""
    s = (s or load_settings()).model_copy(deep=True)   # 실행마다 고쳐 쓰므로 (외부 모델 backend 등) 늘 복사본
    if who is None or not accounts_mod.load_access().login_enabled:
        return s
    base = os.path.normcase(os.path.realpath(s.workspace_path))
    root = accounts_mod.user_workspace(s.workspace_path, who)
    root.mkdir(parents=True, exist_ok=True)
    dirs = []
    for d in s.allowed_dirs:
        p = os.path.normcase(os.path.realpath(s.path(d.path)))
        if p == base:
            dirs.append(config.AllowedDir(path=str(root), mode=d.mode))
        elif not p.startswith(base + os.sep):   # 작업 폴더 밖에 관리자가 따로 허용한 폴더는 그대로
            dirs.append(d)
    s.workspace = str(root)
    s.allowed_dirs = dirs
    if not who.is_admin:   # 하네스 자료는 그 회사가 대상인 폴더만 (agent/harness.py)
        company = accounts.company(who.company_id) if who.company_id else None
        s.audience = company.name if company else ""
    return s


def view_settings(who: User | None) -> config.Settings:
    """화면에서 폴더를 볼 때: 관리자(·혼자 쓰기)는 workspace 전체, 일반 사용자는 자기 작업 폴더만."""
    return load_settings() if accounts_mod.is_admin(who) else settings_for(who)


def allowed_profiles(who: User | None) -> set[str] | None:
    """그 사람이 쓸 수 있는 용도(프로필). None = 전부 (관리자·혼자 쓰기·회사 설정이 비어 있음)."""
    if accounts_mod.is_admin(who) or not who.company_id:   # type: ignore[union-attr]
        return None
    company = accounts.company(who.company_id)   # type: ignore[union-attr]
    return set(company.profiles) if company and company.profiles else None


def run_owner(run_id: str) -> str | None:
    """실행한 사람 id. 막 시작해 아직 기록(DB)에 들어가기 전이면 실행 중인 작업에서 본다. 없는 실행이면 None."""
    handle = runs.get(run_id)
    return handle.owner if handle is not None else store.owner(run_id)


def can_see(owner: str | None, who: User | None) -> bool:
    """실행·대화를 볼 수 있는지: 본인 것이거나 관리자. (혼자 쓰기에서는 전부 — 예전과 같다)"""
    if who is None:
        return True
    return owner is not None and (who.is_admin or owner == who.id)


@dataclass
class RunHandle:
    """실행 하나 (Agent 또는 WorkflowRunner). 이벤트는 메모리에 쌓이고 SSE 로 화면에 흘려보낸다."""
    agent: Any
    events: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, tuple[threading.Event, list[bool]]] = field(default_factory=dict)
    done: bool = False
    auto: AutoMode = "none"   # none = 매번 묻기, writes = 파일 쓰기만 자동, all = 전부 자동
    owner: str = ""           # 실행한 사람 id ('' = 혼자 쓰기)
    local: bool = True        # 이 PC 의 로컬 모델을 쓰는지 (로컬 모델은 한 번에 한 실행씩 — LocalQueue)

    def covers(self, req: ApprovalRequested) -> bool:
        if req.always_ask:   # 되돌릴 수 없는 일(작업·프로젝트 폴더 통째 삭제 등)은 '전부 자동'이어도 사람이 확인한다
            return False
        perm = REGISTRY[req.name].permission if req.name in REGISTRY else "exec"
        return self.auto == "all" or (self.auto == "writes" and perm == "write")

    def approver(self, req: ApprovalRequested) -> bool:
        """실행 스레드에서 호출된다. 자동 승인 범위면 바로 통과, 아니면 화면의 승인/거부(또는 중지)를 기다린다."""
        if self.covers(req):
            return True
        signal, answer = threading.Event(), [False]
        self.pending[req.call_id] = (signal, answer)
        while not signal.wait(0.5):
            if self.agent.cancel.is_set():
                break
            if self.covers(req):          # 기다리는 동안 '전부 자동'으로 바꿨다면 바로 통과
                answer[0] = True
                break
        self.pending.pop(req.call_id, None)
        return answer[0]

    def resolve(self, call_id: str, approved: bool) -> bool:
        item = self.pending.get(call_id)
        if not item:
            return False
        item[1][0] = approved
        item[0].set()
        return True


runs: dict[str, RunHandle] = {}


def active(owner: str | None = None) -> RunHandle | None:
    """지금 실행 중인 작업. owner 를 주면 그 사람의 것만 (한 사람은 한 번에 하나씩 실행한다)."""
    return next((h for h in runs.values() if not h.done and (owner is None or h.owner == owner)), None)


def active_local() -> RunHandle | None:
    """로컬 모델을 쓰는 실행 중인 작업 (모델 교체·모델 서버 끄기를 막는다)."""
    return next((h for h in runs.values() if not h.done and h.local), None)


class LocalQueue:
    """로컬 모델(GPU 하나)은 한 번에 한 실행만 쓴다. 다른 실행은 온 순서대로 기다리고, 기다리는 동안 앞에 몇 개가 있는지 알린다.
    외부 모델 실행은 이 차례와 상관없이 바로 돈다."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.holder: str | None = None
        self.waiting: list[str] = []

    def acquire(self, run_id: str, cancel: threading.Event, on_wait: Callable[[int], None]) -> bool:
        with self.cond:
            self.waiting.append(run_id)
            told = -1
            while self.holder is not None or self.waiting[0] != run_id:
                ahead = self.waiting.index(run_id) + (1 if self.holder is not None else 0)
                if ahead != told:
                    on_wait(ahead)
                    told = ahead
                if cancel.is_set():
                    self.waiting.remove(run_id)
                    self.cond.notify_all()
                    return False
                self.cond.wait(0.5)
            self.waiting.pop(0)
            self.holder = run_id
            return True

    def release(self, run_id: str) -> None:
        with self.cond:
            if self.holder == run_id:
                self.holder = None
            elif run_id in self.waiting:
                self.waiting.remove(run_id)
            self.cond.notify_all()

    def ahead_of(self, run_id: str) -> int:
        with self.cond:
            if run_id not in self.waiting:
                return 0
            return self.waiting.index(run_id) + (1 if self.holder is not None else 0)


local_queue = LocalQueue()
