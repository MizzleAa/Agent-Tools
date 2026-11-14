"""세션 관리자 — 세션 상태 머신, CLI 프로세스 연결, 스트리밍·권한 요청 중계.

세션 상태 (DB sessions.status, 프로세스 상태와 별개):
    IDLE                    대화 가능, 진행 중인 응답 없음 (새 세션 · 사용자가 중단한 뒤)
    RUNNING                 응답 생성 중
    WAITING_FOR_PERMISSION  도구 사용 승인 대기
    WAITING_FOR_INPUT       Claude 가 사용자에게 질문함 (AskUserQuestion) — 답을 기다림
    COMPLETED               마지막 응답이 정상 완료됨
    ERROR                   마지막 응답이 오류로 끝남 / CLI 가 응답 도중 비정상 종료
    DISCONNECTED            응답 도중 백엔드가 꺼져 연결이 끊김 — 다시 보내면 같은 세션으로 재개
CLI 프로세스가 끝나도 세션과 그 기록(Claude Code 가 저장)은 그대로 남는다.

엔진: 세션마다 claude(Claude Agent SDK) · codex · gemini (engines.py 의 드라이버). 세션 상태 · 승인 · 화면 이벤트는 같고,
Codex · Gemini 는 화면 기록을 DB(engine_messages)에 남긴다.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    UserMessage,
)

from .attachments import Attachments
from .cli import CliLocator
from .errors import AppError
from .hub import EventHub
from .procs import ProcessHandle, ProcessManager, external_session_holders
from .settings import ALLOWED_PERMISSION_MODES, Settings
from .store import Database, now_iso
from .transcript import HistoryReader, normalize_sdk

log = logging.getLogger(__name__)

IDLE = "IDLE"
RUNNING = "RUNNING"
WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
WAITING_FOR_PERMISSION = "WAITING_FOR_PERMISSION"
COMPLETED = "COMPLETED"
ERROR = "ERROR"
DISCONNECTED = "DISCONNECTED"
ACTIVE_STATUSES = {RUNNING, WAITING_FOR_INPUT, WAITING_FOR_PERMISSION}

DEFAULT_TITLE = "새 세션"
STALL_SECONDS = 45  # 응답 중인데 CLI 에서 이만큼 아무 소식이 없으면 화면에 알린다 (네트워크 끊김 등)
_UNSET: Any = object()


@dataclass
class Pending:
    id: str
    kind: str  # permission | question | plan
    tool_name: str
    tool_use_id: str | None
    input: dict[str, Any]
    suggestions: list[PermissionUpdate]
    decision_reason: str | None
    blocked_path: str | None
    created_at: float
    future: asyncio.Future

    def to_dict(self) -> dict[str, Any]:
        sugg = []
        for s in self.suggestions:
            try:
                sugg.append(s.to_dict())
            except Exception:  # noqa: BLE001
                sugg.append({"type": getattr(s, "type", "unknown")})
        return {
            "id": self.id,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "tool_use_id": self.tool_use_id,
            "input": self.input,
            "suggestions": sugg,
            "decision_reason": self.decision_reason,
            "blocked_path": self.blocked_path,
            "created_at": self.created_at,
        }


@dataclass
class Turn:
    id: str
    started_at: float
    prompt: dict[str, Any]
    prompt_confirmed: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    partial: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_message: dict[str | None, str | None] = field(default_factory=dict)
    interrupted: bool = False
    status_text: str | None = None
    last_event_at: float = field(default_factory=time.time)
    stall_notified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.id,
            "started_at": self.started_at,
            "prompt": self.prompt,
            "messages": self.messages,
            "partial": list(self.partial.values()),
            "status_text": self.status_text,
            "interrupted": self.interrupted,
        }


@dataclass
class Runner:
    """한 세션에 붙은 살아 있는 CLI 프로세스와 그 실시간 상태."""

    session_id: str
    claude_session_id: str
    handle: ProcessHandle | None = None
    reader: asyncio.Task | None = None
    turn: Turn | None = None
    pending: dict[str, Pending] = field(default_factory=dict)
    last_activity: float = field(default_factory=time.time)
    init_info: dict[str, Any] | None = None
    stopping: bool = False
    cost_base: float = 0.0  # CLI 가 주는 비용은 프로세스 시작부터 누적이라, 응답별 비용을 따로 계산한다
    dead: asyncio.Event = field(default_factory=asyncio.Event)
    engine: str = "claude"
    driver: Any = None      # Codex · Gemini 드라이버 (engines.py) — Claude 는 None (handle.client 를 쓴다)


@dataclass
class EngineHandle:
    """Codex · Gemini 프로세스 1개 (ProcessHandle 과 같은 모양 — 상태 기록·화면 표시에 쓴다)."""

    session_id: str
    pid: int | None
    create_time: float | None
    row_id: int | None
    cli_path: str
    cli_version: str | None
    status: str = "STARTING"
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "pid": self.pid, "status": self.status, "cli_path": self.cli_path,
                "cli_version": self.cli_version, "exit_code": self.exit_code, "registry_id": self.row_id}


class _EngineSink:
    """드라이버 → 화면 이벤트 (Claude Code 와 같은 모양). 드라이버는 이벤트 루프 안에서 부른다."""

    def __init__(self, manager: "SessionManager", runner: Runner):
        self.m, self.r = manager, runner

    def _touch(self) -> None:
        r = self.r
        r.last_activity = time.time()
        if r.turn:
            r.turn.last_event_at = r.last_activity
            if r.turn.stall_notified:
                r.turn.stall_notified = False
                self.m.hub.publish("turn.status", r.session_id, {"status": r.turn.status_text})

    def block_start(self, key: str, btype: str) -> None:
        self._touch()
        if not self.r.turn:
            return
        part = {"key": key, "message_id": key, "index": 0, "type": btype, "text": "", "name": None,
                "tool_use_id": None, "parent_tool_use_id": None}
        self.r.turn.partial[key] = part
        self.m.hub.publish("stream.block_start", self.r.session_id, {"partial": part})

    def delta(self, key: str, text: str) -> None:
        self._touch()
        if self.r.turn and (part := self.r.turn.partial.get(key)) is not None:
            part["text"] += text
        self.m.hub.publish("stream.delta", self.r.session_id, {"key": key, "text": text})

    def drop_partial(self, key: str) -> None:
        if self.r.turn:
            self.r.turn.partial.pop(key, None)

    def message(self, norm: dict[str, Any], replaces_partial: str | None = None) -> None:
        self._touch()
        if self.r.turn:
            if replaces_partial:
                self.r.turn.partial.pop(replaces_partial, None)
            self.r.turn.messages.append(norm)
        self.m.db.add_engine_message(self.r.session_id, norm)
        self.m.hub.publish("message", self.r.session_id, {"message": norm, "replaces_partial": replaces_partial})

    def status(self, text: str | None) -> None:
        self._touch()
        if self.r.turn:
            self.r.turn.status_text = text
        self.m.hub.publish("turn.status", self.r.session_id, {"status": text})

    def notice(self, level: str, text: str) -> None:
        self.m.hub.publish("notice", self.r.session_id, {"level": level, "text": text})

    async def ask(self, tool_name: str, tool_use_id: str, tool_input: dict[str, Any], category: str) -> str:
        return await self.m._engine_ask(self.r, tool_name, tool_use_id, tool_input, category)

    def completed(self, result: dict[str, Any]) -> None:
        self.m._engine_result(self.r, result)

    def exited(self, err: BaseException | None) -> None:
        self.m._on_runner_exit(self.r, err)

    def remember_models(self, models: list[dict[str, Any]]) -> None:
        self.m.db.set_setting(f"{self.r.engine}_models", models)


class SessionManager:
    def __init__(self, db: Database, settings: Settings, hub: EventHub, locator: CliLocator, uploads: Path):
        self.db, self.settings, self.hub, self.locator = db, settings, hub, locator
        self.history = HistoryReader()
        self.attachments = Attachments(uploads)
        self.procs = ProcessManager(db, self._on_process_change)
        self.runners: dict[str, Runner] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sweeper: asyncio.Task | None = None
        self.started_at = time.time()
        self.rate_limit: dict[str, Any] | None = None

    # =====================================================================
    # 수명
    # =====================================================================
    def recover(self) -> dict[str, Any]:
        """백엔드 시작 시: 끊긴 세션 표시, 이전 프로세스 확인. (DB 만 고친다 — Claude 기록은 건드리지 않음)"""
        disconnected = 0
        for s in self.db.list_sessions():
            if s["status"] in ACTIVE_STATUSES:
                self.db.update_session(
                    s["id"], status=DISCONNECTED,
                    last_error="응답 도중 관리 서버가 꺼져 연결이 끊겼습니다. 메시지를 보내면 같은 세션으로 이어서 진행합니다.",
                )
                disconnected += 1
        orphans = self.procs.recover_on_startup()
        log.info("recover: disconnected=%s orphans=%s", disconnected, len(orphans))
        return {"disconnected": disconnected, "orphans": len(orphans)}

    def start_background(self) -> None:
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def shutdown(self, force: bool = True) -> None:
        busy = [sid for sid, r in self.runners.items() if r.turn]
        if busy and not force:
            raise AppError(409, "busy_sessions", "응답을 생성 중인 세션이 있습니다.", {"sessions": busy})
        if self._sweeper:
            self._sweeper.cancel()
        for r in list(self.runners.values()):
            if r.turn:
                await self._interrupt_runner(r, wait=False)
            await self._stop_runner(r)

    def busy_session_ids(self) -> list[str]:
        return [sid for sid, r in self.runners.items() if r.turn]

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(15)
                await self._sweep_once()
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001
                log.exception("sweep failed")

    async def _sweep_once(self) -> None:
        cfg = self.settings.all()
        timeout = cfg["idle_process_timeout_min"] * 60
        now = time.time()
        for r in list(self.runners.values()):
            if not r.turn and not r.pending and now - r.last_activity > timeout:
                log.info("idle timeout: stopping CLI for session %s", r.session_id)
                await self._stop_runner(r)
        for r in list(self.runners.values()):
            t = r.turn
            if t and not r.pending and not t.stall_notified and now - t.last_event_at > STALL_SECONDS:
                t.stall_notified = True
                secs = int(now - t.last_event_at)
                self.hub.publish("turn.status", r.session_id, {"status": "stalled"})
                self.hub.publish("notice", r.session_id, {
                    "level": "warning",
                    "text": f"CLI 응답이 {secs}초 동안 없습니다. 네트워크나 API 상태 문제일 수 있습니다 "
                            "(CLI 가 자동으로 다시 시도하는 중). 계속 기다리거나 중단할 수 있습니다."})
        for sid in set(self.procs.recheck_orphans()):
            self._publish_session(sid)

    # =====================================================================
    # 출력 모양
    # =====================================================================
    def session_out(self, row: dict[str, Any]) -> dict[str, Any]:
        r = self.runners.get(row["id"])
        proc = None
        if r and r.handle:
            proc = {"status": r.handle.status, "pid": r.handle.pid}
        else:
            orphans = [x for x in self.db.live_process_rows() if x["session_id"] == row["id"] and x["status"] == "ORPHANED"]
            if orphans:
                proc = {"status": "ORPHANED", "pid": orphans[-1]["process_id"], "registry_id": orphans[-1]["id"]}
        return {
            **row,
            "has_transcript": bool(row["has_transcript"]),
            "process": proc,
            "pending_count": len(r.pending) if r else 0,
            "busy": bool(r and r.turn),
            "external": False,
        }

    def _publish_session(self, session_id: str) -> None:
        row = self.db.get_session(session_id)
        if row:
            self.hub.publish("session.updated", session_id, {"session": self.session_out(row)})

    def _on_process_change(self, session_id: str, proc: dict[str, Any]) -> None:
        self.hub.publish("process.updated", session_id, {"process": proc})

    def set_status(self, session_id: str, status: str, last_error: Any = _UNSET) -> None:
        fields: dict[str, Any] = {"status": status}
        if last_error is not _UNSET:
            fields["last_error"] = last_error
        self.db.update_session(session_id, **fields)
        self._publish_session(session_id)

    def snapshot(self, session_id: str) -> dict[str, Any]:
        row = self.db.get_session(session_id)
        r = self.runners.get(session_id)
        return {
            "session": self.session_out(row) if row else None,
            "process": r.handle.to_dict() if r and r.handle else None,
            "pending": [p.to_dict() for p in r.pending.values()] if r else [],
            "live_turn": r.turn.to_dict() if r and r.turn else None,
            "init": r.init_info if r else None,
        }

    # =====================================================================
    # 프로세스 연결
    # =====================================================================
    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _require_session(self, session_id: str) -> dict[str, Any]:
        row = self.db.get_session(session_id)
        if not row:
            raise AppError(404, "session_not_found", "세션을 찾을 수 없습니다.")
        return row

    def our_pids(self) -> set[int]:
        return {r.handle.pid for r in self.runners.values() if r.handle and r.handle.pid}

    async def ensure_runner(self, session_id: str, force: bool = False) -> Runner:
        async with self._lock_for(session_id):
            r = self.runners.get(session_id)
            if r and not r.dead.is_set():
                return r
            row = self._require_session(session_id)
            from agent import accounts

            if accounts.load_access().offline:
                raise AppError(409, "offline", "폐쇄망 스위치가 켜져 있어 Claude Code 를 쓸 수 없습니다 (관리 › 접속 설정).")
            wd = Path(row["working_directory"])
            if not wd.is_dir():
                raise AppError(409, "workdir_missing",
                               f"세션의 작업 폴더가 없습니다: {wd}\n폴더를 되돌리거나, 프로젝트 경로를 바꾼 뒤 새 세션을 만드세요.")
            orphans = self.procs.orphans_for(session_id)
            if orphans:
                raise AppError(409, "orphan_running",
                               "이전 관리 서버가 띄운 CLI 프로세스가 아직 이 세션을 쓰고 있습니다. "
                               "끝날 때까지 기다리거나 '이전 프로세스 종료'를 누르세요.",
                               {"orphans": [{"registry_id": o["id"], "pid": o["process_id"]} for o in orphans]})
            engine = row.get("engine") or "claude"
            if engine != "claude":
                await self._make_room()
                return await self._start_engine(row, wd)
            if not force:
                holders = await asyncio.to_thread(external_session_holders, row["claude_session_id"], self.our_pids())
                if holders:
                    raise AppError(409, "session_in_use_elsewhere",
                                   "이 세션은 다른 Claude Code 창(터미널·VS Code)에서 열려 있습니다. "
                                   "같은 세션을 동시에 쓰면 기록이 엇갈릴 수 있으니 그 창을 닫은 뒤 다시 시도하세요.",
                                   {"holders": holders})
            await self._make_room()
            cfg = self.settings.all()
            cli = await asyncio.to_thread(self.locator.locate, cfg["cli_path"])
            if not cli.found or not cli.path:
                raise AppError(503, "cli_not_found", cli.error or "Claude Code CLI 를 찾지 못했습니다.")

            has_t = bool(row["has_transcript"])
            if not has_t and await asyncio.to_thread(self.history.exists, row["claude_session_id"], str(wd)):
                has_t = True
                self.db.update_session(session_id, has_transcript=True)

            runner = Runner(session_id, row["claude_session_id"])
            extra: dict[str, str | None] = {"replay-user-messages": None}
            if not has_t and row["title"] and row["title"] != DEFAULT_TITLE:
                extra["name"] = row["title"]
            from . import instructions   # 로컬·API 와 같은 '설정 · 프롬프트' + 하네스 (agent/loop.py 의 조립 그대로)

            try:
                ins = await asyncio.to_thread(instructions.build, row.get("profile"), wd, None, row.get("harness") or "")
                agents = await asyncio.to_thread(instructions.sdk_agents, instructions.scopes_of(ins["harness"]))
            except Exception as e:  # noqa: BLE001 - 지침을 못 만들면 알리고 진행하지 않는다 (지침 없이 돌리지 않는다)
                raise AppError(500, "instructions_failed", f"설정 · 프롬프트 / 하네스 지침을 만들지 못했습니다: {e}") from e
            harness_dirs = [d for d in ins["dirs"] if Path(d).is_dir()]   # 붙는 하네스(공통 + 고른 것) 폴더를 읽을 수 있게
            opts = ClaudeAgentOptions(
                system_prompt={"type": "preset", "preset": "claude_code", "append": ins["text"]} if ins["text"] else None,
                agents=agents,
                effort=row.get("effort") or None,
                cli_path=cli.path,
                cwd=str(wd),
                model=row["model"] or None,
                permission_mode=row["permission_mode"] if row["permission_mode"] in ALLOWED_PERMISSION_MODES else "default",
                include_partial_messages=True,
                can_use_tool=lambda name, inp, ctx: self._can_use_tool(runner, name, inp, ctx),
                resume=row["claude_session_id"] if has_t else None,
                session_id=None if has_t else row["claude_session_id"],
                add_dirs=[str(self.attachments.dir_for(session_id)), *harness_dirs],
                settings=json.dumps({"claudeMdExcludes": instructions.dev_md_excludes()}, ensure_ascii=False),
                extra_args=extra,
            )
            try:
                runner.handle = await self.procs.start(session_id, opts, cli.version)
            except Exception as e:  # noqa: BLE001
                tail = list(self.procs.stderr_tail.get(session_id, []))[-8:]
                log.warning("CLI start failed session=%s: %s", session_id, e)
                raise AppError(502, "cli_start_failed", f"Claude Code CLI 를 시작하지 못했습니다: {e}", {"stderr": tail}) from e
            runner.reader = asyncio.create_task(self._read_loop(runner))
            self.runners[session_id] = runner
            self._publish_session(session_id)
            return runner

    async def _start_engine(self, row: dict[str, Any], wd: Path) -> Runner:
        """Codex · Gemini: 같은 지침(설정 · 프롬프트 + 하네스)을 붙여 CLI 를 띄우고, 재개면 그 CLI 의 대화를 이어받는다."""
        from . import engines, instructions

        session_id, engine = row["id"], row["engine"]
        try:
            ins = await asyncio.to_thread(instructions.build, row.get("profile"), wd, None, row.get("harness") or "", engine)
        except Exception as e:  # noqa: BLE001 - 지침 없이 돌리지 않는다
            raise AppError(500, "instructions_failed", f"설정 · 프롬프트 / 하네스 지침을 만들지 못했습니다: {e}") from e
        runner = Runner(session_id, row["claude_session_id"], engine=engine)
        cfg = self.settings.all()
        try:
            runner.driver = await asyncio.to_thread(engines.make_driver, engine, _EngineSink(self, runner), wd,
                                                    cfg.get(f"{engine}_path") or "")
        except engines.EngineError as e:
            raise AppError(e.status, e.code, e.message) from e
        cli = runner.driver.cli
        handle = EngineHandle(session_id, None, None, None, cli.path or engines.LABELS[engine], cli.version)
        runner.handle = handle
        self._on_process_change(session_id, handle.to_dict())
        try:
            eid = await runner.driver.start(
                resume=row["claude_session_id"] if row["has_transcript"] else None, instructions=ins["text"],
                model=row["model"], effort=row.get("effort"), mode=row["permission_mode"],
                dirs=[str(self.attachments.dir_for(session_id)), *ins["dirs"]])
        except engines.EngineError as e:
            self.procs.set_status(handle, "CRASHED")   # type: ignore[arg-type]
            raise AppError(e.status, e.code, e.message, {"stderr": runner.driver.stderr_tail[-8:]}) from e
        handle.pid = runner.driver.pid
        if handle.pid:
            try:
                import psutil
                handle.create_time = psutil.Process(handle.pid).create_time()
            except Exception:  # noqa: BLE001
                pass
        handle.row_id = self.db.add_process(session_id, handle.pid or 0, "READY", handle.cli_path, handle.cli_version,
                                            handle.create_time)
        self.procs.set_status(handle, "READY")   # type: ignore[arg-type]
        if eid != row["claude_session_id"]:
            self.db.update_session(session_id, claude_session_id=eid)
            runner.claude_session_id = eid
        r_model = runner.driver.model
        runner.init_info = {"model": r_model, "cwd": str(wd), "permission_mode": row["permission_mode"],
                            "claude_code_version": f"{engines.LABELS[engine]} {cli.version or ''}".strip(),
                            "tools": 0, "mcp_servers": [], "api_key_source": None}
        self.hub.publish("system.init", session_id, {"init": runner.init_info})
        self.runners[session_id] = runner
        self._publish_session(session_id)
        return runner

    async def _make_room(self) -> None:
        limit = self.settings.get("max_processes")
        while len(self.runners) >= limit:
            idle = sorted((r for r in self.runners.values() if not r.turn and not r.pending), key=lambda r: r.last_activity)
            if not idle:
                raise AppError(429, "too_many_processes",
                               f"동시에 실행 중인 CLI 프로세스가 최대치({limit})입니다. 다른 세션의 응답이 끝난 뒤 다시 시도하세요.")
            await self._stop_runner(idle[0])

    async def _stop_runner(self, r: Runner) -> None:
        r.stopping = True
        for p in list(r.pending.values()):
            if not p.future.done():
                p.future.set_result(PermissionResultDeny(message="세션 프로세스가 종료되었습니다.", interrupt=True))
        if r.driver:
            await r.driver.stop()
            if r.handle:
                self.procs.set_status(r.handle, "EXITED", r.driver.returncode)
        elif r.handle and r.handle.status not in ("EXITED", "CRASHED"):
            await self.procs.stop(r.handle)
        if r.reader:
            try:
                await asyncio.wait_for(r.reader, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                r.reader.cancel()
        if self.runners.get(r.session_id) is r:
            self.runners.pop(r.session_id, None)
        if r.turn:
            r.turn = None
            row = self.db.get_session(r.session_id)
            if row and row["status"] in ACTIVE_STATUSES:
                self.set_status(r.session_id, IDLE, "응답 도중 프로세스를 종료했습니다.")
        self._publish_session(r.session_id)

    async def _read_loop(self, r: Runner) -> None:
        err: BaseException | None = None
        try:
            assert r.handle
            async for msg in r.handle.client.receive_messages():
                r.last_activity = time.time()
                if r.turn:
                    r.turn.last_event_at = r.last_activity
                    if r.turn.stall_notified:
                        r.turn.stall_notified = False
                        self.hub.publish("turn.status", r.session_id, {"status": r.turn.status_text})
                try:
                    self._dispatch(r, msg)
                except Exception:  # noqa: BLE001
                    log.exception("dispatch failed session=%s", r.session_id)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001
            err = e
        finally:
            self._on_runner_exit(r, err)

    def _on_runner_exit(self, r: Runner, err: BaseException | None) -> None:
        r.dead.set()
        if self.runners.get(r.session_id) is r:
            self.runners.pop(r.session_id, None)
        for p in list(r.pending.values()):
            if not p.future.done():
                p.future.cancel()
        r.pending.clear()
        if r.stopping:
            return
        if r.driver:
            code = r.driver.returncode
            tail = r.driver.stderr_tail[-8:]
        else:
            code = self.procs.returncode(r.handle) if r.handle else None
            tail = list(self.procs.stderr_tail.get(r.session_id, []))[-8:]
        if r.handle:
            self.procs.set_status(r.handle, "CRASHED" if (err or (code not in (0, None))) else "EXITED", code)
        detail = f"{type(err).__name__}: {err}" if err else f"exit code {code}"
        log.warning("CLI exited unexpectedly session=%s (%s)", r.session_id, detail)
        if r.turn:
            turn, r.turn = r.turn, None
            label = {"codex": "Codex", "gemini": "Gemini"}.get(r.engine, "Claude Code")
            msg = (f"{label} CLI 프로세스가 응답 도중 종료되었습니다 ({detail}). "
                   "다시 보내면 같은 세션을 재개해 이어서 진행합니다.")
            self.hub.publish("turn.error", r.session_id, {"turn_id": turn.id, "error": msg, "stderr": tail})
            self.set_status(r.session_id, ERROR, msg)
        else:
            self._publish_session(r.session_id)

    # =====================================================================
    # CLI 메시지 처리
    # =====================================================================
    def _dispatch(self, r: Runner, msg: Any) -> None:
        sid = r.session_id
        if isinstance(msg, StreamEvent):
            self._on_stream(r, msg)
        elif isinstance(msg, AssistantMessage):
            norm = normalize_sdk(msg)
            replaces = None
            if r.turn:
                btype = norm["blocks"][0]["type"] if norm["blocks"] else None
                for k, p in r.turn.partial.items():
                    if p["message_id"] == msg.message_id and p["type"] == btype:
                        replaces = k
                        break
                if replaces:
                    r.turn.partial.pop(replaces, None)
                r.turn.messages.append(norm)
            self.hub.publish("message", sid, {"message": norm, "replaces_partial": replaces})
            if msg.error:
                self.hub.publish("notice", sid, {"level": "error", "text": f"API 오류: {msg.error}"})
        elif isinstance(msg, UserMessage):
            norm = normalize_sdk(msg)
            replaces_local = None
            if r.turn and not r.turn.prompt_confirmed and msg.parent_tool_use_id is None and norm["kind"] in ("prompt", "meta"):
                r.turn.prompt_confirmed = True
                replaces_local = r.turn.prompt.get("local_id")
                norm["local_id"] = replaces_local
                r.turn.prompt = norm
                row = self.db.get_session(sid)
                if row and not row["has_transcript"]:
                    self.db.update_session(sid, has_transcript=True)
            elif r.turn:
                r.turn.messages.append(norm)
            self.hub.publish("message", sid, {"message": norm, "replaces_local": replaces_local})
        elif isinstance(msg, ResultMessage):
            self._on_result(r, msg)
        elif isinstance(msg, SystemMessage):
            self._on_system(r, msg)
        elif isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            self.rate_limit = {"status": info.status, "utilization": info.utilization, "type": info.rate_limit_type,
                               "resets_at": info.resets_at}
            self.hub.publish("rate_limit", sid, {"rate_limit": self.rate_limit})
            if info.status == "rejected":
                self.hub.publish("notice", sid, {"level": "error", "text": "사용량 한도에 도달해 요청이 거절되었습니다.",
                                                 "resets_at": info.resets_at})

    def _on_stream(self, r: Runner, msg: StreamEvent) -> None:
        turn = r.turn
        if not turn:
            return
        ev = msg.event or {}
        t = ev.get("type")
        ptu = msg.parent_tool_use_id
        if t == "message_start":
            turn.current_message[ptu] = (ev.get("message") or {}).get("id")
        elif t == "content_block_start":
            cb = ev.get("content_block") or {}
            mid = turn.current_message.get(ptu)
            key = f"{mid}:{ev.get('index')}"
            btype = {"text": "text", "thinking": "thinking", "redacted_thinking": "thinking",
                     "tool_use": "tool_use", "server_tool_use": "tool_use"}.get(cb.get("type"), cb.get("type"))
            part = {"key": key, "message_id": mid, "index": ev.get("index"), "type": btype, "text": "",
                    "name": cb.get("name"), "tool_use_id": cb.get("id"), "parent_tool_use_id": ptu}
            turn.partial[key] = part
            self.hub.publish("stream.block_start", r.session_id, {"partial": part})
        elif t == "content_block_delta":
            d = ev.get("delta") or {}
            dt = d.get("type")
            text = (d.get("text") if dt == "text_delta" else d.get("thinking") if dt == "thinking_delta"
                    else d.get("partial_json") if dt == "input_json_delta" else None)
            if not text:
                return
            key = f"{turn.current_message.get(ptu)}:{ev.get('index')}"
            part = turn.partial.get(key)
            if part is not None:
                part["text"] += text
            self.hub.publish("stream.delta", r.session_id, {"key": key, "text": text})

    def _on_system(self, r: Runner, msg: SystemMessage) -> None:
        sid = r.session_id
        data = msg.data or {}
        if msg.subtype == "init":
            r.init_info = {
                "model": data.get("model"),
                "cwd": data.get("cwd"),
                "permission_mode": data.get("permissionMode"),
                "claude_code_version": data.get("claude_code_version"),
                "tools": len(data.get("tools") or []),
                "mcp_servers": [{"name": m.get("name"), "status": m.get("status")} for m in (data.get("mcp_servers") or [])
                                if isinstance(m, dict)],
                "api_key_source": data.get("apiKeySource"),
            }
            mode = data.get("permissionMode")
            row = self.db.get_session(sid)
            if row and mode in ALLOWED_PERMISSION_MODES and row["permission_mode"] != mode:
                self.db.update_session(sid, permission_mode=mode)
                self._publish_session(sid)
            self.hub.publish("system.init", sid, {"init": r.init_info})
        elif msg.subtype == "status":
            if r.turn:
                r.turn.status_text = data.get("status")
            self.hub.publish("turn.status", sid, {"status": data.get("status")})
        elif msg.subtype == "compact_boundary":
            self.hub.publish("notice", sid, {"level": "info", "text": "대화가 길어 이전 내용을 요약(compact)했습니다."})
        elif msg.subtype == "api_retry":
            # CLI 가 API 호출 실패 후 자동으로 다시 시도하는 중 (네트워크 끊김·과부하 등)
            retry = {"attempt": data.get("attempt"), "max_retries": data.get("max_retries"),
                     "delay_ms": data.get("retry_delay_ms"), "error_status": data.get("error_status")}
            first = bool(r.turn) and r.turn.status_text != "retrying"
            if r.turn:
                r.turn.status_text = "retrying"
            self.hub.publish("turn.status", sid, {"status": "retrying", "retry": retry})
            if first:
                code = f" (HTTP {retry['error_status']})" if retry["error_status"] else ""
                self.hub.publish("notice", sid, {"level": "warning", "text": f"Claude API 연결에 실패해 CLI 가 자동으로 다시 시도하는 중입니다{code}. "
                                                                         "네트워크 상태를 확인하거나 Esc 로 중단할 수 있습니다."})
        elif "error" in (msg.subtype or ""):
            self.hub.publish("notice", sid, {"level": "warning", "text": f"CLI: {msg.subtype} {str(data)[:200]}"})
        elif msg.subtype in ("task_started", "task_notification"):
            desc = getattr(msg, "description", None) or data.get("description") or data.get("summary") or ""
            if desc:
                self.hub.publish("notice", sid, {"level": "info", "text": f"백그라운드 작업: {desc}"})

    def _on_result(self, r: Runner, msg: ResultMessage) -> None:
        sid = r.session_id
        turn, r.turn = r.turn, None
        if r.handle:
            self.procs.set_status(r.handle, "READY")
        interrupted = bool(turn and turn.interrupted) or (msg.terminal_reason or "").startswith("aborted")
        result = {
            "turn_id": turn.id if turn else None,
            "subtype": msg.subtype,
            "is_error": msg.is_error,
            "interrupted": interrupted,
            "duration_ms": msg.duration_ms,
            "num_turns": msg.num_turns,
            "total_cost_usd": msg.total_cost_usd,
            "turn_cost_usd": (max(0.0, msg.total_cost_usd - r.cost_base) if msg.total_cost_usd is not None else None),
            "usage": msg.usage,
            "result": msg.result if isinstance(msg.result, str) else None,
            "errors": msg.errors,
            "terminal_reason": msg.terminal_reason,
            "permission_denials": len(msg.permission_denials or []),
        }
        if msg.total_cost_usd is not None:
            r.cost_base = msg.total_cost_usd
        self.db.update_session(sid, has_transcript=True, last_used_at=self._now())
        if interrupted:
            status, err = IDLE, None
        elif msg.is_error:
            status = ERROR
            err = "; ".join(msg.errors or []) or (msg.result if isinstance(msg.result, str) else None) or msg.subtype
            if msg.api_error_status:
                err = f"API {msg.api_error_status}: {err}"
        else:
            status, err = COMPLETED, None
        self.hub.publish("turn.completed", sid, {"result": result})
        self.set_status(sid, status, err)

    def _engine_result(self, r: Runner, res: dict[str, Any]) -> None:
        """Codex · Gemini 응답 끝 → _on_result 와 같은 처리."""
        sid = r.session_id
        turn, r.turn = r.turn, None
        if r.handle:
            self.procs.set_status(r.handle, "READY")
        interrupted = bool(res.get("interrupted") or (turn and turn.interrupted))
        result = {"turn_id": turn.id if turn else None, "subtype": res.get("subtype"), "is_error": bool(res.get("is_error")),
                  "interrupted": interrupted, "duration_ms": res.get("duration_ms"), "num_turns": 1,
                  "total_cost_usd": None, "turn_cost_usd": None, "usage": res.get("usage"), "result": res.get("result"),
                  "errors": res.get("errors") or [], "terminal_reason": res.get("terminal_reason"), "permission_denials": 0}
        self.db.update_session(sid, has_transcript=True, last_used_at=self._now())
        if interrupted:
            status, err = IDLE, None
            self.db.add_engine_message(sid, {"uuid": uuid.uuid4().hex, "role": "user", "kind": "notice",
                                             "blocks": [{"type": "text", "text": "[Request interrupted by user]"}], "model": None,
                                             "message_id": None, "parent_tool_use_id": None})
        elif res.get("is_error"):
            status, err = ERROR, "; ".join(result["errors"]) or str(res.get("subtype") or "오류")
        else:
            status, err = COMPLETED, None
        self.hub.publish("turn.completed", sid, {"result": result})
        self.set_status(sid, status, err)

    async def _engine_ask(self, r: Runner, tool_name: str, tool_use_id: str, tool_input: dict[str, Any], category: str) -> str:
        """Codex · Gemini 의 승인 요청 → 화면 승인 패널. 'allow' · 'deny' · 'interrupt'.
        권한 모드: acceptEdits = 파일 고치기만 자동 허용 · plan(Codex) = 실행·고치기를 모두 거절 (읽기 전용 명령은 Codex 가 묻지 않고 돈다)."""
        row = self.db.get_session(r.session_id) or {}
        mode = row.get("permission_mode") or "default"
        if mode == "acceptEdits" and category == "edit":
            return "allow"
        if mode == "plan" and r.engine == "codex" and category in ("edit", "command"):
            return "deny"
        p = Pending(id=uuid.uuid4().hex[:12], kind="permission", tool_name=tool_name, tool_use_id=tool_use_id,
                    input=tool_input, suggestions=[], decision_reason=None, blocked_path=None, created_at=time.time(),
                    future=asyncio.get_running_loop().create_future())
        r.pending[p.id] = p
        self.set_status(r.session_id, WAITING_FOR_PERMISSION)
        self.hub.publish("permission.requested", r.session_id, {"permission": p.to_dict()})
        decision = "deny"
        try:
            res = await p.future
            if isinstance(res, PermissionResultAllow):
                decision = "allow"
            elif getattr(res, "interrupt", False):
                decision = "interrupt"
            return decision
        except asyncio.CancelledError:
            decision = "interrupt"
            return decision
        finally:
            r.pending.pop(p.id, None)
            r.last_activity = time.time()
            self.hub.publish("permission.resolved", r.session_id, {"permission_id": p.id,
                                                                   "decision": "allow" if decision == "allow" else "deny"})
            if r.turn and not r.pending and not r.stopping:
                self.set_status(r.session_id, RUNNING)

    @staticmethod
    def _now() -> str:
        return now_iso()

    # =====================================================================
    # 권한 요청
    # =====================================================================
    async def _can_use_tool(self, r: Runner, tool_name: str, tool_input: dict[str, Any], ctx: Any) -> Any:
        kind = "question" if tool_name == "AskUserQuestion" else "plan" if tool_name == "ExitPlanMode" else "permission"
        loop = asyncio.get_running_loop()
        p = Pending(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            tool_name=tool_name,
            tool_use_id=getattr(ctx, "tool_use_id", None),
            input=tool_input,
            suggestions=list(getattr(ctx, "suggestions", None) or []),
            decision_reason=getattr(ctx, "decision_reason", None),
            blocked_path=getattr(ctx, "blocked_path", None),
            created_at=time.time(),
            future=loop.create_future(),
        )
        r.pending[p.id] = p
        self.set_status(r.session_id, WAITING_FOR_INPUT if kind == "question" else WAITING_FOR_PERMISSION)
        self.hub.publish("permission.requested", r.session_id, {"permission": p.to_dict()})
        decision = "deny"
        try:
            result = await p.future
            decision = "allow" if isinstance(result, PermissionResultAllow) else "deny"
            return result
        except asyncio.CancelledError:
            return PermissionResultDeny(message="세션이 종료되어 승인 요청이 취소되었습니다.", interrupt=True)
        finally:
            r.pending.pop(p.id, None)
            r.last_activity = time.time()
            self.hub.publish("permission.resolved", r.session_id, {"permission_id": p.id, "decision": decision})
            if r.turn and not r.pending and not r.stopping:
                self.set_status(r.session_id, RUNNING)

    def resolve_permission(self, session_id: str, perm_id: str, behavior: str, message: str | None = None,
                           apply_suggestions: list[int] | None = None, answers: dict[str, str] | None = None,
                           updated_input: dict[str, Any] | None = None, interrupt: bool = False) -> None:
        r = self.runners.get(session_id)
        p = r.pending.get(perm_id) if r else None
        if not p or p.future.done():
            raise AppError(404, "permission_not_found", "이미 처리되었거나 없는 승인 요청입니다.")
        if behavior == "allow":
            new_input = None
            if p.kind == "question":
                new_input = {**p.input, "answers": answers or {}}
            elif updated_input is not None:
                new_input = updated_input
            perms = [p.suggestions[i] for i in (apply_suggestions or []) if 0 <= i < len(p.suggestions)]
            p.future.set_result(PermissionResultAllow(updated_input=new_input, updated_permissions=perms or None))
        else:
            p.future.set_result(PermissionResultDeny(message=message or "사용자가 거부했습니다.", interrupt=interrupt))

    # =====================================================================
    # 사용자 동작
    # =====================================================================
    async def open_session(self, session_id: str) -> dict[str, Any]:
        """화면에서 세션을 열 때: 최근 사용 기록 + (설정 시) CLI 미리 띄우기."""
        row = self._require_session(session_id)
        self.db.update_session(session_id, last_used_at=self._now())
        self.db.touch_project(row["project_id"], session_id)
        if self.settings.get("prewarm_process") and session_id not in self.runners:
            asyncio.create_task(self._prewarm(session_id))
        return self.session_out(self.db.get_session(session_id))  # type: ignore[arg-type]

    async def _prewarm(self, session_id: str) -> None:
        try:
            await self.ensure_runner(session_id)
        except AppError as e:
            self.hub.publish("notice", session_id, {"level": "warning", "text": e.message, "code": e.code})
        except Exception as e:  # noqa: BLE001
            log.warning("prewarm failed: %s", e)

    async def send_message(self, session_id: str, text: str, attachment_ids: list[str], force: bool = False) -> dict[str, Any]:
        row = self._require_session(session_id)
        files = self.attachments.resolve(session_id, attachment_ids)
        if not text.strip() and not files:
            raise AppError(400, "empty_message", "보낼 내용이 없습니다.")
        r = await self.ensure_runner(session_id, force=force)
        if r.turn:
            raise AppError(409, "busy", "이 세션은 응답을 생성하는 중입니다. 끝나거나 중단한 뒤 보내세요.")

        content, blocks = self._build_content(text, files)
        local_id = "local-" + uuid.uuid4().hex[:10]
        prompt = {"uuid": None, "local_id": local_id, "role": "user", "kind": "prompt", "blocks": blocks,
                  "model": None, "message_id": None, "parent_tool_use_id": None}
        turn = Turn(id=uuid.uuid4().hex, started_at=time.time(), prompt=prompt)
        r.turn = turn
        r.last_activity = time.time()
        if r.handle:
            self.procs.set_status(r.handle, "BUSY")
        if row["title"] == DEFAULT_TITLE and text.strip():
            title = " ".join(text.split())[:60]
            self.db.update_session(session_id, title=title)
        self.db.update_session(session_id, last_used_at=self._now())
        self.db.touch_project(row["project_id"], session_id)
        self.set_status(session_id, RUNNING, None)
        self.hub.publish("turn.started", session_id, {"turn_id": turn.id, "prompt": prompt})
        if r.driver:
            return await self._engine_send(r, turn, row, text, files, prompt, local_id)

        message = {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None,
                   "session_id": r.claude_session_id}

        async def one():
            yield message

        try:
            assert r.handle
            await r.handle.client.query(one())
        except Exception as e:  # noqa: BLE001
            r.turn = None
            if r.handle:
                self.procs.set_status(r.handle, "READY")
            msg = f"메시지를 CLI 로 보내지 못했습니다: {e}"
            self.hub.publish("turn.error", session_id, {"turn_id": turn.id, "error": msg})
            self.set_status(session_id, ERROR, msg)
            raise AppError(502, "send_failed", msg) from e
        return {"turn_id": turn.id, "local_id": local_id}

    async def _engine_send(self, r: Runner, turn: Turn, row: dict[str, Any], text: str, files: list[dict[str, Any]],
                           prompt: dict[str, Any], local_id: str) -> dict[str, Any]:
        sid = r.session_id
        others = [f for f in files if f["kind"] != "image"]
        images = [{"path": f["path"], "mime": f["mime"], "data": base64.b64encode(Path(f["path"]).read_bytes()).decode()}
                  for f in files if f["kind"] == "image"]
        body = text
        if others:
            lines = "\n".join(f"- {f['path']}" for f in others)
            body = (body + "\n\n" if body else "") + f"[첨부 파일 — 열어서 읽어 보세요]\n{lines}"
        try:
            await r.driver.send(body, images, not row["has_transcript"])
        except Exception as e:  # noqa: BLE001
            r.turn = None
            if r.handle:
                self.procs.set_status(r.handle, "READY")
            msg = f"메시지를 CLI 로 보내지 못했습니다: {getattr(e, 'message', None) or e}"
            self.hub.publish("turn.error", sid, {"turn_id": turn.id, "error": msg})
            self.set_status(sid, ERROR, msg)
            raise AppError(502, "send_failed", msg) from e
        confirmed = {**prompt, "uuid": turn.id}
        turn.prompt, turn.prompt_confirmed = confirmed, True
        stored = {k: v for k, v in confirmed.items() if k != "local_id"}
        stored["blocks"] = [({**b, "data": None, "omitted": True} if b.get("type") == "image" and len(b.get("data") or "") > 1_500_000 else b)
                            for b in stored["blocks"]]
        self.db.add_engine_message(sid, stored)
        self.hub.publish("message", sid, {"message": confirmed, "replaces_local": local_id})
        return {"turn_id": turn.id, "local_id": local_id}

    def history_page(self, row: dict[str, Any], limit: int, before: int | None) -> dict[str, Any]:
        """대화 기록 한 쪽: Claude Code 는 그 CLI 의 기록, Codex · Gemini 는 우리 DB."""
        if (row.get("engine") or "claude") != "claude":
            return self.db.engine_messages(row["id"], limit, before)
        return self.history.page(row["claude_session_id"], row["working_directory"], limit, before)

    def _build_content(self, text: str, files: list[dict[str, Any]]) -> tuple[Any, list[dict[str, Any]]]:
        images = [f for f in files if f["kind"] == "image"]
        others = [f for f in files if f["kind"] != "image"]
        body = text
        if others:
            lines = "\n".join(f"- {f['path']}" for f in others)
            body = (body + "\n\n" if body else "") + f"[첨부 파일 — Read 도구로 열어 보세요]\n{lines}"
        if not images:
            return body, [{"type": "text", "text": body}]
        content: list[dict[str, Any]] = []
        shown: list[dict[str, Any]] = []
        if body:
            content.append({"type": "text", "text": body})
            shown.append({"type": "text", "text": body})
        for f in images:
            data = base64.b64encode(Path(f["path"]).read_bytes()).decode()
            content.append({"type": "image", "source": {"type": "base64", "media_type": f["mime"], "data": data}})
            shown.append({"type": "image", "media_type": f["mime"], "data": data})
        return content, shown

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        self._require_session(session_id)
        r = self.runners.get(session_id)
        if not r or not r.turn:
            return {"interrupted": False}
        await self._interrupt_runner(r, wait=False)
        return {"interrupted": True}

    async def _interrupt_runner(self, r: Runner, wait: bool) -> None:
        turn = r.turn
        if not turn:
            return
        turn.interrupted = True
        for p in list(r.pending.values()):
            if not p.future.done():
                p.future.set_result(PermissionResultDeny(message="사용자가 중단했습니다.", interrupt=True))
        try:
            if r.driver:
                await asyncio.wait_for(r.driver.interrupt(), timeout=10)
            else:
                assert r.handle
                await asyncio.wait_for(r.handle.client.interrupt(), timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("interrupt failed session=%s: %s", r.session_id, e)
        if wait:
            return
        asyncio.create_task(self._interrupt_watchdog(r, turn.id))

    async def _interrupt_watchdog(self, r: Runner, turn_id: str, grace: float = 20.0) -> None:
        """중단 요청 뒤에도 응답이 끝나지 않으면 프로세스를 정리한다 (세션 기록은 남는다)."""
        await asyncio.sleep(grace)
        if r.turn and r.turn.id == turn_id and not r.dead.is_set():
            log.warning("interrupt did not finish in %ss; stopping CLI session=%s", grace, r.session_id)
            await self._stop_runner(r)

    async def stop_process(self, session_id: str) -> None:
        self._require_session(session_id)
        r = self.runners.get(session_id)
        if not r:
            return
        if r.turn:
            await self._interrupt_runner(r, wait=True)
        await self._stop_runner(r)

    async def start_process(self, session_id: str, force: bool = False) -> dict[str, Any]:
        r = await self.ensure_runner(session_id, force=force)
        return r.handle.to_dict() if r.handle else {}

    async def update_session(self, session_id: str, title: str | None = None, model: str | None = _UNSET,
                             permission_mode: str | None = None, effort: str | None = _UNSET,
                             profile: str | None = _UNSET, harness: str | None = _UNSET) -> dict[str, Any]:
        row = self._require_session(session_id)
        fields: dict[str, Any] = {}
        restart = False
        if effort is not _UNSET and (effort or None) != row.get("effort"):
            fields["effort"] = effort or None
            restart = True   # 추론 강도는 CLI 를 띄울 때 정한다 → 쉬는 프로세스를 닫아 다음 메시지부터 새 값
        if profile is not _UNSET and (profile or None) != row.get("profile"):
            if row["has_transcript"]:
                raise AppError(409, "profile_locked", "이미 시작한 대화의 용도는 바꿀 수 없습니다 (CLI 가 첫 요청의 지침을 기록해 계속 씁니다). 새 대화에서 고르세요.")
            fields["profile"] = profile or None
            restart = True
        if harness is not _UNSET and (harness or None) != row.get("harness"):
            if row["has_transcript"]:
                raise AppError(409, "harness_locked", "이미 시작한 대화의 하네스는 바꿀 수 없습니다 (CLI 가 첫 요청의 지침을 기록해 계속 씁니다). 새 대화에서 고르세요.")
            fields["harness"] = harness or None
            restart = True
        if title is not None and title.strip():
            fields["title"] = title.strip()[:200]
        if permission_mode is not None:
            if permission_mode not in ALLOWED_PERMISSION_MODES:
                raise AppError(400, "bad_permission_mode", "허용되지 않는 권한 모드입니다.")
            fields["permission_mode"] = permission_mode
        if model is not _UNSET:
            fields["model"] = (model or "").strip() or None
        self.db.update_session(session_id, **fields)
        r = self.runners.get(session_id)
        if restart and r:
            if r.turn:
                self.hub.publish("notice", session_id, {"level": "info", "text": "추론 강도·용도는 지금 응답이 끝난 뒤 다음 메시지부터 적용됩니다."})
            else:
                await self._stop_runner(r)
                r = None
        if r and r.handle and not r.dead.is_set():
            try:
                target = r.driver or r.handle.client
                if "permission_mode" in fields:
                    await (r.driver.set_mode(fields["permission_mode"]) if r.driver else target.set_permission_mode(fields["permission_mode"]))
                if "model" in fields:
                    await target.set_model(fields["model"])
            except Exception as e:  # noqa: BLE001
                log.warning("live update failed: %s", e)
                self.hub.publish("notice", session_id, {"level": "warning",
                                                        "text": f"실행 중인 CLI 에 바로 적용하지 못했습니다 (다음 시작 때 적용): {e}"})
        self._publish_session(session_id)
        return self.session_out(self.db.get_session(session_id))  # type: ignore[arg-type]

    async def delete_session(self, session_id: str) -> None:
        """GUI 목록에서만 지운다. Claude Code 의 세션 기록 파일은 그대로 둔다."""
        self._require_session(session_id)
        r = self.runners.get(session_id)
        if r:
            if r.turn:
                await self._interrupt_runner(r, wait=True)
            await self._stop_runner(r)
        self.db.delete_session(session_id)
        self.attachments.remove_session(session_id)
        self.hub.publish("session.deleted", session_id, {"session_id": session_id})

    def terminate_orphan(self, registry_id: int) -> dict[str, Any]:
        row = self.procs.terminate_orphan(registry_id)
        if not row:
            raise AppError(404, "process_not_found", "종료할 이전 프로세스가 없습니다.")
        self._publish_session(row["session_id"])
        return row
