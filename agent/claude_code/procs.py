"""Claude Code CLI 프로세스 관리.

프로세스 생명주기 (세션 상태와는 별개):
    STARTING → READY ⇄ BUSY → STOPPING → EXITED
                         └──────────────→ CRASHED   (예상하지 못한 종료)
    백엔드가 다시 켜졌을 때 이전 기록의 PID 가 아직 살아 있으면 ORPHANED, 이미 없으면 LOST.

규칙
- 화면(GUI 창)을 닫아도 CLI 프로세스는 백엔드가 쥐고 있으므로 계속 돈다.
- 할 일이 없는 프로세스는 설정한 시간이 지나면 정상 종료한다 (세션 기록은 CLI 가 이미 저장).
- 같은 Claude 세션을 두 프로세스가 동시에 쓰지 않게 한다: 고아 프로세스나 다른 Claude Code 창에서
  같은 세션이 실행 중이면 새로 띄우지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psutil
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from .store import Database

log = logging.getLogger(__name__)

ALIVE_STATES = ("STARTING", "READY", "BUSY", "STOPPING")


def proc_matches(pid: int, create_time: float | None) -> bool:
    """pid 가 살아 있고 기록한 시작 시각과 같은(재사용되지 않은) 프로세스인지."""
    try:
        p = psutil.Process(pid)
        if create_time is not None and abs(p.create_time() - create_time) > 2.0:
            return False
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


def kill_tree(pid: int, timeout: float = 5.0) -> bool:
    """프로세스와 그 자식(셸 명령·MCP 서버 등)을 함께 끝낸다."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    procs = [parent]
    try:
        procs += parent.children(recursive=True)
    except psutil.Error:
        pass
    for p in reversed(procs):
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return True


def external_session_holders(claude_session_id: str, exclude_pids: set[int]) -> list[dict[str, Any]]:
    """다른 Claude Code 창(터미널·VS Code)에서 같은 세션이 실행 중인지 읽기 전용으로 확인한다.

    ~/.claude/sessions/<pid>.json 은 CLI 내부 파일이라 형식이 바뀔 수 있다 — 읽지 못하면 조용히 넘어간다.
    """
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "sessions"
    out: list[dict[str, Any]] = []
    try:
        files = list(base.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("sessionId") != claude_session_id:
            continue
        pid = d.get("pid")
        if not isinstance(pid, int) or pid in exclude_pids or not psutil.pid_exists(pid):
            continue
        try:
            name = psutil.Process(pid).name().lower()
        except psutil.Error:
            continue
        if "claude" not in name and "node" not in name:
            continue
        out.append({"pid": pid, "kind": d.get("kind"), "entrypoint": d.get("entrypoint"), "cwd": d.get("cwd")})
    return out


@dataclass
class ProcessHandle:
    """살아 있는 CLI 프로세스 1개 (SDK 클라이언트를 감싼다)."""

    session_id: str
    client: ClaudeSDKClient
    pid: int | None
    create_time: float | None
    row_id: int | None
    cli_path: str
    cli_version: str | None
    status: str = "STARTING"
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "status": self.status,
            "cli_path": self.cli_path,
            "cli_version": self.cli_version,
            "exit_code": self.exit_code,
            "registry_id": self.row_id,
        }


class ProcessManager:
    def __init__(self, db: Database, on_change: Callable[[str, dict[str, Any]], None]):
        self.db = db
        self._on_change = on_change
        self.stderr_tail: dict[str, deque[str]] = {}

    # ---- 시작/종료 -----------------------------------------------------------
    async def start(self, session_id: str, options: ClaudeAgentOptions, cli_version: str | None) -> ProcessHandle:
        tail: deque[str] = deque(maxlen=40)
        self.stderr_tail[session_id] = tail
        options.stderr = lambda line: tail.append(line.rstrip())
        client = ClaudeSDKClient(options)
        handle = ProcessHandle(session_id, client, None, None, None, str(options.cli_path), cli_version)
        self._emit(handle)
        await client.connect()
        proc = getattr(getattr(client, "_transport", None), "_process", None)
        pid = getattr(proc, "pid", None)
        ctime = None
        if pid:
            try:
                ctime = psutil.Process(pid).create_time()
            except psutil.Error:
                pass
        handle.pid, handle.create_time = pid, ctime
        handle.row_id = self.db.add_process(session_id, pid or 0, "READY", handle.cli_path, cli_version, ctime)
        handle.status = "READY"
        self._emit(handle)
        log.info("CLI started session=%s pid=%s", session_id, pid)
        return handle

    def set_status(self, handle: ProcessHandle, status: str, exit_code: int | None = None) -> None:
        if handle.status == status and exit_code is None:
            return
        handle.status = status
        if exit_code is not None:
            handle.exit_code = exit_code
        if handle.row_id:
            fields: dict[str, Any] = {"status": status}
            if exit_code is not None:
                fields["exit_code"] = exit_code
            self.db.update_process(handle.row_id, **fields)
        self._emit(handle)

    def returncode(self, handle: ProcessHandle) -> int | None:
        proc = getattr(getattr(handle.client, "_transport", None), "_process", None)
        return getattr(proc, "returncode", None)

    async def stop(self, handle: ProcessHandle) -> None:
        """정상 종료: stdin 을 닫고 CLI 가 스스로 끝나기를 기다린다 (SDK 가 시간 초과 시 terminate)."""
        self.set_status(handle, "STOPPING")
        try:
            await handle.client.disconnect()
        except Exception as e:  # noqa: BLE001
            log.warning("disconnect error session=%s: %s", handle.session_id, e)
        if handle.pid and proc_matches(handle.pid, handle.create_time):
            kill_tree(handle.pid)
        self.set_status(handle, "EXITED", self.returncode(handle))

    def _emit(self, handle: ProcessHandle) -> None:
        self._on_change(handle.session_id, handle.to_dict())

    # ---- 백엔드 재시작 후 정리 ----------------------------------------------
    def recover_on_startup(self) -> list[dict[str, Any]]:
        """이전 실행에서 살아 있다고 기록된 프로세스를 확인: 살아 있으면 ORPHANED, 없으면 LOST."""
        orphans = []
        for row in self.db.live_process_rows():
            if row["process_id"] and proc_matches(row["process_id"], row["proc_create_time"]):
                self.db.update_process(row["id"], status="ORPHANED")
                orphans.append({**row, "status": "ORPHANED"})
            else:
                self.db.update_process(row["id"], status="LOST")
        self.db.prune_processes()
        return orphans

    def orphans_for(self, session_id: str) -> list[dict[str, Any]]:
        rows = [r for r in self.db.live_process_rows() if r["session_id"] == session_id and r["status"] == "ORPHANED"]
        alive = []
        for r in rows:
            if proc_matches(r["process_id"], r["proc_create_time"]):
                alive.append(r)
            else:
                self.db.update_process(r["id"], status="LOST")
        return alive

    def recheck_orphans(self) -> list[str]:
        """사라진 고아 프로세스를 LOST 로 바꾸고, 바뀐 세션 id 를 돌려준다."""
        changed = []
        for r in self.db.live_process_rows():
            if r["status"] == "ORPHANED" and not proc_matches(r["process_id"], r["proc_create_time"]):
                self.db.update_process(r["id"], status="LOST")
                changed.append(r["session_id"])
        return changed

    def terminate_orphan(self, registry_id: int) -> dict[str, Any] | None:
        row = self.db.get_process(registry_id)
        if not row or row["status"] != "ORPHANED":
            return None
        if proc_matches(row["process_id"], row["proc_create_time"]):
            kill_tree(row["process_id"])
        self.db.update_process(registry_id, status="EXITED")
        return self.db.get_process(registry_id)
