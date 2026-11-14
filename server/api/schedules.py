"""예약 실행 API (9단계) — 정해 둔 시각 · 간격에 요청을 실행한다. 대화 화면 '/예약' 과 사이드바 '예약' 이 쓴다.

    GET    /api/v1/schedules              내 예약 (관리자 · 혼자 쓰기는 전부)
    POST   /api/v1/schedules              만들기 — 요청 + 시각 규칙 + 입력창 선택(용도 · 모델 · 승인 …)
    PATCH  /api/v1/schedules/{id}         켜기/끄기 · 시각 · 요청 바꾸기
    DELETE /api/v1/schedules/{id}
    POST   /api/v1/schedules/{id}/run     지금 한 번 실행

실행은 그 예약을 만든 사람으로, 보통 실행(/api/v1/runs)과 같은 길(start_run)로 한다. 그 사람이 이미 실행 중이면 건너뛴다
(줄 세우지 않는다 — 다음 시각에 다시). 관리 서버가 켜져 있는 동안 30초마다 tick 이 다음 실행 시각이 된 것을 돌린다.
CLI 모델(Claude Code · Codex · Gemini)은 예약할 수 없다 (승인을 사람이 그 자리에서 해야 하므로).
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from agent import accounts as accounts_mod
from agent import schedules
from server import deps
from server.api.common import ERRORS, ApiModel, Ok

router = APIRouter(tags=["schedules"], responses=ERRORS)
TICK_SECONDS = 30


class ScheduleWhen(ApiModel):
    kind: Literal["once", "daily", "weekdays", "weekly", "every"] = Field(
        description="once 한 번 · daily 매일 · weekdays 평일 · weekly 고른 요일 · every N분마다")
    at: str = Field("09:00", description="시각 HH:MM (every 는 쓰지 않음)")
    date: str = Field("", description="once 의 날짜 YYYY-MM-DD")
    days: list[int] = Field(default_factory=list, description="weekly 의 요일 (0=월 … 6=일)")
    minutes: int = Field(60, description="every 의 간격 (분, 5 이상)")


class ScheduleOptions(ApiModel):
    """입력창에서 고른 것 (보통 실행과 같은 뜻)."""
    profile: str = "general"
    workflow: str = ""
    model: str = Field("", description="'' = 로컬 모델 · 'ext:…' = 외부 모델 (CLI 는 안 됨)")
    effort: Literal["", "low", "medium", "high", "xhigh", "max"] = ""
    harness: str = ""
    auto_approve: deps.AutoMode = Field("none", description="예약 실행은 사람이 없을 때 돌므로 'none' 이면 승인을 기다리며 멈춘다")
    max_steps: int | None = Field(None, ge=0)


class ScheduleIn(ApiModel):
    task: str
    when: ScheduleWhen
    options: ScheduleOptions = Field(default_factory=ScheduleOptions)


class ScheduleUpdate(ApiModel):
    task: str | None = None
    when: ScheduleWhen | None = None
    options: ScheduleOptions | None = None
    enabled: bool | None = None


class ScheduleOut(ApiModel):
    id: str
    title: str
    task: str
    when: ScheduleWhen
    when_text: str = Field(description="사람이 읽는 규칙 (예: 매주 월·수 18:30)")
    options: ScheduleOptions
    enabled: bool
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_run_id: str = Field("", description="마지막으로 시작한 실행 (그 대화로 갈 때)")
    last_status: str = Field("", description="시작함 · 건너뜀: 까닭")
    owner: str = Field("", description="만든 사람 아이디 ('' = 혼자 쓰기)")
    created_at: str


def store():
    return deps.store


def _iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def _out(row: dict[str, Any]) -> ScheduleOut:
    when = json.loads(row["when_json"])
    owner = ""
    if row["user_id"]:
        u = deps.accounts.user(row["user_id"])
        owner = u.username if u else row["user_id"]
    return ScheduleOut(id=row["id"], title=row["title"], task=row["task"], when=ScheduleWhen(**when), when_text=schedules.describe(when),
                       options=ScheduleOptions(**json.loads(row["options_json"] or "{}")), enabled=bool(row["enabled"]),
                       next_run_at=row["next_run_at"], last_run_at=row["last_run_at"], last_run_id=row["last_run_id"] or "",
                       last_status=row["last_status"] or "", owner=owner, created_at=row["created_at"])


def _validate(task: str, when: ScheduleWhen, options: ScheduleOptions) -> dict[str, Any]:
    if not task.strip():
        raise HTTPException(400, "예약할 요청을 적으세요")
    try:
        schedules.When.parse(when.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if options.model.split(":", 1)[0] in ("cc", "cx", "gm"):
        raise HTTPException(400, "CLI 모델(Claude Code · Codex · Gemini)은 예약할 수 없습니다 — 로컬 모델이나 외부 API 모델을 고르세요")
    return when.model_dump()


def _mine(schedule_id: str, request: Request) -> dict[str, Any]:
    row = store().get_schedule(schedule_id)
    who = deps.user(request)
    if row is None or not (accounts_mod.is_admin(who) or row["user_id"] == accounts_mod.owner_id(who)):
        raise HTTPException(404, "예약이 없습니다")   # 남의 예약은 있는지도 알 수 없게
    return row


@router.get("/schedules", response_model=list[ScheduleOut], summary="예약 목록 (로그인 사용: 내 것만 · 관리자는 전부)")
def list_schedules(request: Request) -> list[ScheduleOut]:
    who = deps.user(request)
    rows = store().list_schedules(None if accounts_mod.is_admin(who) else accounts_mod.owner_id(who))
    return [_out(r) for r in rows]


@router.post("/schedules", response_model=ScheduleOut, summary="예약 만들기 (요청 + 시각 규칙 + 입력창 선택)")
def create_schedule(body: ScheduleIn, request: Request) -> ScheduleOut:
    when = _validate(body.task, body.when, body.options)
    nxt = schedules.next_fire(when, datetime.now())
    if nxt is None:
        raise HTTPException(400, "이미 지난 시각입니다 — 앞으로의 날짜 · 시각을 고르세요")
    row = store().add_schedule({"id": uuid.uuid4().hex[:12], "user_id": accounts_mod.owner_id(deps.user(request)),
                                "title": body.task.strip().splitlines()[0][:80], "task": body.task.strip(),
                                "when_json": json.dumps(when, ensure_ascii=False),
                                "options_json": body.options.model_dump_json(), "next_run_at": _iso(nxt)})
    return _out(row)


@router.patch("/schedules/{schedule_id}", response_model=ScheduleOut, summary="예약 바꾸기 (켜기/끄기 · 시각 · 요청 · 선택)")
def update_schedule(schedule_id: str, body: ScheduleUpdate, request: Request) -> ScheduleOut:
    row = _mine(schedule_id, request)
    task = body.task if body.task is not None else row["task"]
    when = body.when or ScheduleWhen(**json.loads(row["when_json"]))
    options = body.options or ScheduleOptions(**json.loads(row["options_json"] or "{}"))
    when_d = _validate(task, when, options)
    enabled = body.enabled if body.enabled is not None else bool(row["enabled"])
    nxt = schedules.next_fire(when_d, datetime.now()) if enabled else None
    if enabled and nxt is None:
        raise HTTPException(400, "이미 지난 시각입니다 — 앞으로의 날짜 · 시각을 고르세요")
    store().update_schedule(schedule_id, task=task.strip(), title=task.strip().splitlines()[0][:80],
                            when_json=json.dumps(when_d, ensure_ascii=False), options_json=options.model_dump_json(),
                            enabled=int(enabled), next_run_at=_iso(nxt))
    return _out(store().get_schedule(schedule_id))  # type: ignore[arg-type]


@router.delete("/schedules/{schedule_id}", response_model=Ok, summary="예약 지우기")
def delete_schedule(schedule_id: str, request: Request) -> Ok:
    _mine(schedule_id, request)
    store().delete_schedule(schedule_id)
    return Ok()


@router.post("/schedules/{schedule_id}/run", response_model=ScheduleOut, summary="예약을 지금 한 번 실행 (다음 시각은 그대로)")
def run_now(schedule_id: str, request: Request) -> ScheduleOut:
    row = _mine(schedule_id, request)
    run_id, status = fire(row)
    store().update_schedule(schedule_id, last_run_at=_iso(datetime.now()), last_run_id=run_id, last_status=status)
    return _out(store().get_schedule(schedule_id))  # type: ignore[arg-type]


# ───────────────────────── 실행

def fire(row: dict[str, Any]) -> tuple[str, str]:
    """그 예약을 만든 사람으로 실행을 시작한다. (실행 id, 상태 글)."""
    from server.api import runs

    who = None
    if row["user_id"]:
        who = deps.accounts.user(row["user_id"])
        if who is None:
            return "", "건너뜀: 만든 사람의 계정이 없습니다"
    opts = ScheduleOptions(**json.loads(row["options_json"] or "{}"))
    body = runs.RunCreate(task=row["task"], profile=opts.profile, workflow=opts.workflow, model=opts.model, effort=opts.effort,
                          harness=opts.harness, auto_approve=opts.auto_approve, max_steps=opts.max_steps)
    token = accounts_mod.CURRENT.set(who)
    try:
        ref = runs.start_run(body, SimpleNamespace(state=SimpleNamespace(user=who)))   # type: ignore[arg-type]
        return ref.run_id, "시작함"
    except HTTPException as e:
        return "", f"건너뜀: {e.detail}"
    except Exception as e:  # noqa: BLE001 - 예약 하나의 문제로 다른 예약이 멈추면 안 된다
        return "", f"건너뜀: {type(e).__name__}: {e}"
    finally:
        accounts_mod.CURRENT.reset(token)


def tick(now: datetime | None = None) -> list[str]:
    """다음 실행 시각이 된 예약을 돌린다. 오래 놓친 시각(관리 서버가 꺼져 있던 동안)은 건너뛰고 다음으로. 돌린 예약 id."""
    now = (now or datetime.now()).replace(microsecond=0)
    fired = []
    for row in store().due_schedules(_iso(now)):
        when = json.loads(row["when_json"])
        due = datetime.fromisoformat(row["next_run_at"])
        nxt = schedules.next_fire(when, now)
        fields: dict[str, Any] = {"next_run_at": _iso(nxt)}
        if now - due > schedules.GRACE:
            fields["last_status"] = f"건너뜀: 관리 서버가 꺼져 있던 시각 ({due:%m-%d %H:%M})"
        else:
            run_id, status = fire(row)
            fields.update(last_run_at=_iso(now), last_run_id=run_id, last_status=status)
            fired.append(row["id"])
        if nxt is None:
            fields["enabled"] = 0   # 한 번만 · 지난 시각
        store().update_schedule(row["id"], **fields)
    return fired


_stop = threading.Event()
_thread: threading.Thread | None = None


def start_loop() -> None:
    """관리 서버가 켜질 때 (server/app.py)."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()

    def loop() -> None:
        while not _stop.wait(TICK_SECONDS):
            try:
                tick()
            except Exception:  # noqa: BLE001 - 다음 tick 에 다시
                import logging
                logging.getLogger(__name__).exception("schedule tick failed")

    _thread = threading.Thread(target=loop, name="schedules", daemon=True)
    _thread.start()


def stop_loop() -> None:
    _stop.set()
