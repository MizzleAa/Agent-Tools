"""Claude Code 세션 매니저 API (관리자만): 관리 › Claude Code 탭.

    /api/v1/claude-code/status · settings · processes             상태·설정·CLI 프로세스 (폴더 고르기는 /admin/fs/dirs)
    /api/v1/claude-code/projects …                                프로젝트 (추가·변경·삭제·세션 목록·가져오기)
    /api/v1/claude-code/sessions …                                세션 (열기·기록·보내기·중단·승인·첨부·프로세스)
    /api/v1/claude-code/events?session_id=                        실시간 이벤트 (text/event-stream)

오류는 다른 API 와 같이 {"detail": 설명}. 화면이 경우를 나눠야 하는 오류는 X-Error-Code(+ X-Error-Extra JSON) 머리글을 붙인다.
엔진은 agent/claude_code/ (claude-agent-sdk 가 없으면 상태만 '설치 필요'로 답하고 나머지는 503).
모든 처리기가 async 인 까닭: 엔진의 백그라운드 작업이 관리 서버의 이벤트 루프에서 돌아야 한다.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from agent import accounts
from server.api.common import ERRORS, ApiModel, Ok

router = APIRouter(prefix="/claude-code", tags=["claude-code"], responses=ERRORS)

INSTALL_HINT = "Claude Code 기능 패키지가 설치되어 있지 않습니다: .venv\\Scripts\\pip install -r requirements-claude-code.txt"
PermissionMode = Literal["default", "acceptEdits", "plan"]
Effort = Literal["", "low", "medium", "high", "xhigh", "max"]   # CLI 의 --effort ("" = CLI 기본)
Engine = Literal["claude", "codex", "gemini"]   # CLI 엔진 (agent/claude_code/engines.py)
SessionStatus = Literal["IDLE", "RUNNING", "WAITING_FOR_INPUT", "WAITING_FOR_PERMISSION", "COMPLETED", "ERROR", "DISCONNECTED"]


def _svc():
    try:
        from agent.claude_code.service import get_service
    except ImportError as e:
        raise HTTPException(503, INSTALL_HINT) from e
    return get_service()


def installed() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


# ───────────────────────── 모양

class ProjectOut(ApiModel):
    id: str
    name: str
    path: str
    path_exists: bool
    created_at: str
    updated_at: str
    last_used_at: str | None = None
    last_session_id: str | None = None
    session_count: int = 0


class ProjectCreate(ApiModel):
    path: str = Field(min_length=1)
    name: str | None = None


class ProjectUpdate(ApiModel):
    name: str | None = None
    path: str | None = None


class ProjectUpdateOut(ApiModel):
    project: ProjectOut
    sessions_on_old_path: int = Field(0, description="경로를 바꿔도 기존 세션은 원래 작업 폴더를 유지한다")


class ProcessBrief(ApiModel):
    status: str
    pid: int | None = None
    registry_id: int | None = None


class CcSession(ApiModel):
    id: str
    project_id: str
    claude_session_id: str
    title: str
    status: SessionStatus
    working_directory: str
    model: str | None = None
    permission_mode: str
    has_transcript: bool
    origin: str
    last_error: str | None = None
    created_at: str
    updated_at: str
    last_used_at: str | None = None
    process: ProcessBrief | None = None
    pending_count: int = 0
    busy: bool = False
    profile: str | None = Field(None, description="용도 (설정 · 프롬프트의 프로필) — CLI 에 붙는 지침. null = 기본(general)")
    effort: str | None = Field(None, description="추론 강도 (CLI --effort). null = CLI 기본")
    harness: str | None = Field(None, description="회사·프로젝트 하네스 (공통 + 이것이 붙는다). null = 공통만")
    engine: Engine = Field("claude", description="CLI 엔진: claude(Claude Code) · codex(OpenAI) · gemini(Google)")


class ExternalSession(ApiModel):
    claude_session_id: str
    title: str
    first_prompt: str | None = None
    last_modified: int
    created_at: int | None = None
    cwd: str | None = None
    git_branch: str | None = None
    file_size: int | None = None


class ProjectSessions(ApiModel):
    sessions: list[CcSession]
    external: list[ExternalSession]
    external_error: str | None = None


class SessionCreate(ApiModel):
    engine: Engine = "claude"
    title: str | None = None
    model: str | None = None
    permission_mode: PermissionMode | None = None
    profile: str = Field("", description="용도 — 로컬·API 와 같은 '설정 · 프롬프트'와 하네스 지침을 붙인다 ('' = general)")
    effort: Effort = ""
    harness: str = Field("", description="회사·프로젝트 하네스 — 공통 하네스에 더해 붙는다 ('' = 공통만, GET /harnesses)")


class ConversationCreate(ApiModel):
    engine: Engine = Field("claude", description="CLI 엔진: claude(Claude Code) · codex(OpenAI Codex) · gemini(Google Gemini CLI)")
    task: str = Field("", description="첫 요청 — 작업 폴더 이름에 쓴다 (에이전트의 새 작업과 같은 규칙)")
    base_dir: str = Field("", description="작업 위치: 기준 폴더(환경변수 AGENT_WORK_ROOT, 없으면 workspace) 아래 상대 경로 — 로컬 에이전트와 같은 규칙")
    model: str | None = None
    permission_mode: PermissionMode | None = None
    profile: str = Field("", description="용도 — 로컬·API 와 같은 '설정 · 프롬프트'와 하네스 지침을 붙인다 ('' = general)")
    effort: Effort = ""
    harness: str = Field("", description="회사·프로젝트 하네스 — 공통 하네스에 더해 붙는다 ('' = 공통만, GET /harnesses)")


class SessionImport(ApiModel):
    claude_session_id: str


class SessionUpdate(ApiModel):
    title: str | None = None
    model: str | None = Field(None, description="빈 문자열 또는 null = CLI 기본 모델")
    permission_mode: PermissionMode | None = None
    effort: Effort | None = Field(None, description="추론 강도 — 다음 메시지부터 (쉬는 CLI 를 다시 띄운다)")
    profile: str | None = Field(None, description="용도 — 아직 보내기 전인 대화만 바꿀 수 있다")
    harness: str | None = Field(None, description="하네스 — 아직 보내기 전인 대화만 바꿀 수 있다")


class InstructionSection(ApiModel):
    id: str
    title: str
    text: str


class InstructionRole(ApiModel):
    name: str
    description: str


class CliInstructions(ApiModel):
    profile: str
    harness: str = Field(description="붙는 회사·프로젝트 하네스 ('' = 공통만)")
    sections: list[InstructionSection]
    text: str = Field(description="Claude Code 기본 시스템 프롬프트 뒤에 덧붙는 글")
    roles: list[InstructionRole] = Field(description="Claude Code 서브에이전트로 넘기는 하네스 역할")


class MessagesPage(ApiModel):
    total: int
    start: int
    end: int
    messages: list[dict[str, Any]]


class SendMessage(ApiModel):
    text: str = ""
    attachments: list[str] = []
    force: bool = Field(False, description="다른 Claude Code 창에서 같은 세션이 열려 있어도 보낸다")


class SendResult(ApiModel):
    turn_id: str
    local_id: str


class InterruptResult(ApiModel):
    interrupted: bool


class PermissionDecision(ApiModel):
    behavior: Literal["allow", "deny"]
    message: str | None = None
    apply_suggestions: list[int] = []
    answers: dict[str, str] | None = None
    interrupt: bool = False


class AttachmentIn(ApiModel):
    name: str
    mime: str = ""
    data_base64: str = Field(description="파일 내용 (base64)")


class AttachmentOut(ApiModel):
    id: str
    name: str
    size: int
    mime: str
    kind: Literal["image", "file"]
    path: str


class ProcessStartIn(ApiModel):
    force: bool = False


class ProcessInfo(ApiModel):
    session_id: str
    pid: int | None = None
    status: str
    cli_path: str
    cli_version: str | None = None
    exit_code: int | None = None
    registry_id: int | None = None


class CliCommand(ApiModel):
    name: str = Field(description="'/' 뒤 이름 (예: compact)")
    description: str = ""
    description_en: str = Field("", description="CLI 가 준 원문 설명 (자주 쓰는 명령은 description 이 한국어)")
    hint: str = Field("", description="인자 힌트 (예: [issue description])")
    aliases: list[str] = []
    builtin: bool = False


class CliCommands(ApiModel):
    engine: Engine
    source: Literal["session", "cache", "default", "none"] = Field(
        description="session: 그 대화의 CLI 가 준 것 · cache: 그 엔진이 마지막으로 준 것 · default: 기본 목록 · none: 없음 (Codex)")
    commands: list[CliCommand]


class Snapshot(ApiModel):
    session: CcSession | None
    process: dict[str, Any] | None = None
    pending: list[dict[str, Any]] = []
    live_turn: dict[str, Any] | None = None
    init: dict[str, Any] | None = None
    commands: list[CliCommand] | None = None


class CcSettings(ApiModel):
    cli_path: str
    codex_path: str = Field("", description="Codex CLI 경로 (비우면 자동 탐색)")
    gemini_path: str = Field("", description="Gemini CLI 경로 (비우면 자동 탐색)")
    default_model: str
    default_permission_mode: PermissionMode
    prewarm_process: bool
    idle_process_timeout_min: int
    max_processes: int
    show_thinking: bool
    show_external_sessions: bool
    send_with_enter: bool


class CcSettingsUpdate(ApiModel):
    cli_path: str | None = None
    codex_path: str | None = None
    gemini_path: str | None = None
    default_model: str | None = None
    default_permission_mode: PermissionMode | None = None
    prewarm_process: bool | None = None
    idle_process_timeout_min: int | None = None
    max_processes: int | None = None
    show_thinking: bool | None = None
    show_external_sessions: bool | None = None
    send_with_enter: bool | None = None


class CliCandidate(ApiModel):
    path: str
    source: str


class CliInfo(ApiModel):
    found: bool
    path: str | None = None
    source: str | None = None
    version: str | None = None
    error: str | None = None
    candidates: list[CliCandidate] = []


class CliAuth(ApiModel):
    loggedIn: bool = False
    authMethod: str | None = None
    apiProvider: str | None = None
    email: str | None = None
    orgName: str | None = None
    subscriptionType: str | None = None
    error: str | None = None


class EngineModel(ApiModel):
    id: str
    label: str | None = None
    default: bool = False
    efforts: list[str] = []


class EngineLogin(ApiModel):
    state: Literal["idle", "waiting", "done", "failed"] = "idle"
    url: str | None = Field(None, description="이 PC 의 브라우저에서 열 로그인 주소")
    error: str | None = None


class EngineInstall(ApiModel):
    state: Literal["idle", "running", "done", "failed"] = "idle"
    error: str | None = None
    log: str | None = None


class EngineInfo(ApiModel):
    engine: Engine
    label: str
    installed: bool
    path: str | None = None
    source: str | None = None
    version: str | None = None
    error: str | None = None
    logged_in: bool = False
    login_method: str | None = None
    account: str | None = None
    models: list[EngineModel] = Field(default_factory=list, description="고를 수 있는 모델 (Codex 는 CLI 가 알려 줌, Gemini 는 한 번 써 본 뒤)")
    login: EngineLogin = Field(default_factory=EngineLogin)
    install: EngineInstall = Field(default_factory=EngineInstall)


class CcStatus(ApiModel):
    installed: bool
    offline: bool = Field(description="폐쇄망 스위치 (켜져 있으면 Claude Code 를 쓸 수 없다)")
    sdk_version: str | None = None
    cli: CliInfo | None = None
    auth: CliAuth | None = None
    alive: int = 0
    busy: int = 0
    max_processes: int = 0
    orphaned: int = 0
    rate_limit: dict[str, Any] | None = None
    home: str | None = None
    message: str | None = None
    engines: list[EngineInfo] = Field(default_factory=list, description="Claude Code 말고 쓸 수 있는 CLI 엔진 (Codex · Gemini)")


class ProcessRow(ApiModel):
    registry_id: int
    session_id: str
    session_title: str | None = None
    project_name: str | None = None
    pid: int
    status: str
    cli_version: str | None = None
    exit_code: int | None = None
    started_at: str
    updated_at: str
    live: bool


# ───────────────────────── 상태 · 설정 · 프로세스 · 폴더

@router.get("/status", response_model=CcStatus, summary="Claude Code 연결 상태 (설치·CLI·로그인·프로세스)")
async def status(refresh: bool = False) -> dict[str, Any]:
    offline = accounts.load_access().offline
    if not installed():
        return {"installed": False, "offline": offline, "message": INSTALL_HINT}
    from claude_agent_sdk import __version__ as sdk_version

    s = _svc()
    cfg = s.settings.all()
    cli = await asyncio.to_thread(s.locator.locate, cfg["cli_path"], refresh)
    auth = await asyncio.to_thread(s.locator.auth_status, cli.path if cli.found else None, refresh)
    return {
        "installed": True, "offline": offline, "sdk_version": sdk_version, "cli": cli.to_dict(), "auth": auth,
        "alive": len(s.manager.runners), "busy": len(s.manager.busy_session_ids()), "max_processes": cfg["max_processes"],
        "orphaned": sum(1 for r in s.db.live_process_rows() if r["status"] == "ORPHANED"),
        "rate_limit": s.manager.rate_limit, "home": str(s.home), "engines": await _engines(refresh),
    }


async def _engines(refresh: bool = False) -> list[dict[str, Any]]:
    """Codex · Gemini 상태 (설치·로그인·모델). 로그인 상태는 프로세스를 띄워 읽으므로 10분 캐시 — 로그인·설치하면 새로."""
    from agent.claude_code import engines

    s = _svc()
    cfg, stored = s.settings.all(), s.db.get_settings()
    out = []
    for engine in ("codex", "gemini"):
        info = dict(await engines.status_cache.get(engine, cfg.get(f"{engine}_path") or "", refresh, ttl=600))
        if not info.get("models") and isinstance(stored.get(f"{engine}_models"), list):
            info["models"] = stored[f"{engine}_models"]
        info["login"] = engines.logins.status(engine)
        info["install"] = engines.installer.status(engine)
        out.append(info)
    return out


@router.post("/engines/{engine}/login", response_model=EngineLogin,
             summary="Codex · Gemini 로그인 시작 — 받은 주소를 이 PC 의 브라우저에서 열어 로그인 (한 번이면 계속 쓴다)")
async def engine_login(engine: Literal["codex", "gemini"]) -> dict[str, Any]:
    from agent.claude_code import engines

    if accounts.load_access().offline:
        raise HTTPException(409, "폐쇄망 스위치가 켜져 있어 로그인할 수 없습니다")
    try:
        return await engines.logins.start(engine, _svc().settings.get(f"{engine}_path") or "")
    except engines.EngineError as e:
        raise HTTPException(e.status, e.message) from e


@router.post("/engines/{engine}/install", response_model=EngineInstall,
             summary="Codex · Gemini CLI 를 이 PC 에 설치 (Agent/runtime/<엔진>-cli 에 npm — 전역 설정은 그대로)")
async def engine_install(engine: Literal["codex", "gemini"]) -> dict[str, Any]:
    from agent.claude_code import engines

    if accounts.load_access().offline:
        raise HTTPException(409, "폐쇄망 스위치가 켜져 있어 설치할 수 없습니다")
    try:
        return await engines.installer.install(engine)
    except engines.EngineError as e:
        raise HTTPException(e.status, e.message) from e


@router.get("/settings", response_model=CcSettings, summary="Claude Code 탭 설정")
async def get_settings() -> dict[str, Any]:
    return _svc().settings.all()


@router.put("/settings", response_model=CcSettings, summary="Claude Code 탭 설정 저장")
async def put_settings(body: CcSettingsUpdate) -> dict[str, Any]:
    s = _svc()
    values = body.model_dump(exclude_unset=True)
    for key in ("cli_path", "codex_path", "gemini_path"):
        if values.get(key):
            p = values[key].strip().strip('"')
            if not Path(p).is_file():
                raise HTTPException(400, f"파일이 없습니다: {p}")
            values[key] = p
    if {"codex_path", "gemini_path"} & set(values):
        from agent.claude_code import engines
        engines.status_cache.clear()
    out = s.settings.update(values)
    if "cli_path" in values:
        await asyncio.to_thread(s.locator.locate, out["cli_path"], True)
    return out


@router.get("/processes", response_model=list[ProcessRow], summary="CLI 프로세스 (실행 중 + 최근 기록)")
async def processes() -> list[dict[str, Any]]:
    from agent.claude_code.procs import proc_matches

    s = _svc()
    sessions = {r["id"]: r for r in s.db.list_sessions()}
    projects = {p["id"]: p for p in s.db.list_projects()}
    out = []
    for r in s.db.recent_processes(80):
        sess = sessions.get(r["session_id"])
        live = r["status"] in ("STARTING", "READY", "BUSY", "STOPPING", "ORPHANED") and proc_matches(r["process_id"], r["proc_create_time"])
        out.append({"registry_id": r["id"], "session_id": r["session_id"], "session_title": sess["title"] if sess else None,
                    "project_name": projects.get(sess["project_id"], {}).get("name") if sess else None, "pid": r["process_id"],
                    "status": r["status"], "cli_version": r["cli_version"], "exit_code": r["exit_code"],
                    "started_at": r["started_at"], "updated_at": r["updated_at"], "live": live})
    return out


@router.post("/processes/{registry_id}/terminate", response_model=Ok, summary="이전 서버가 남긴 CLI 프로세스 종료")
async def terminate_orphan(registry_id: int) -> Ok:
    _svc().manager.terminate_orphan(registry_id)
    return Ok()


@router.get("/instructions", response_model=CliInstructions,
            summary="CLI 에 붙는 지침 보기 — 로컬·API 와 같은 '설정 · 프롬프트'(용도·블록)와 하네스")
async def cli_instructions(profile: str = "", session_id: str | None = None, harness: str = "") -> dict[str, Any]:
    """session_id 를 주면 그 대화의 용도·작업 폴더 기준 (작업 폴더 안의 AGENT.md 등도 들어간다)."""
    from agent.claude_code import instructions

    workdir = None
    if session_id:
        row = _svc().manager._require_session(session_id)
        profile, workdir, harness = row.get("profile") or "", Path(row["working_directory"]), row.get("harness") or ""
    else:   # 대화의 하네스가 그 뒤 지워졌으면 공통만으로 보여 준다 (instructions.build 가 뺀다)
        _check_harness(harness)
    try:
        return await asyncio.to_thread(instructions.build, profile or None, workdir, None, harness)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"지침을 만들지 못했습니다: {e}") from e


@router.get("/commands", response_model=CliCommands,
            summary="CLI 의 '/' 명령 목록 — 입력창에서 / 를 치면 보여 준다 (session_id 를 주면 그 대화의 CLI 기준)")
async def cli_commands(engine: Engine = "claude", session_id: str | None = None) -> dict[str, Any]:
    return _svc().manager.commands_for(engine, session_id)


def _check_harness(harness_id: str) -> None:
    from agent import harness

    if harness_id and harness.get_scope(harness_id) is None:
        raise HTTPException(404, f"없는 하네스입니다: {harness_id}")


# ───────────────────────── 프로젝트

@router.get("/projects", response_model=list[ProjectOut], summary="프로젝트 목록 (최근 사용 순)")
async def list_projects() -> list[dict[str, Any]]:
    s = _svc()
    return [s.project_out(p) for p in s.db.list_projects()]


@router.post("/projects", response_model=ProjectOut, status_code=201, summary="프로젝트 추가 (폴더)")
async def create_project(body: ProjectCreate) -> dict[str, Any]:
    return _svc().create_project(body.path, body.name)


@router.patch("/projects/{project_id}", response_model=ProjectUpdateOut, summary="프로젝트 이름·경로 변경")
async def update_project(project_id: str, body: ProjectUpdate) -> dict[str, Any]:
    return _svc().update_project(project_id, body.name, body.path)


@router.delete("/projects/{project_id}", response_model=Ok, summary="프로젝트를 목록에서 삭제 (폴더·기록은 그대로)")
async def delete_project(project_id: str) -> Ok:
    await _svc().delete_project(project_id)
    return Ok()


@router.get("/projects/{project_id}/sessions", response_model=ProjectSessions, summary="프로젝트의 세션 + 가져올 수 있는 Claude Code 기록")
async def project_sessions(project_id: str, include_external: bool = True) -> dict[str, Any]:
    s = _svc()
    return await s.project_sessions(project_id, include_external and s.settings.get("show_external_sessions"))


@router.post("/projects/{project_id}/sessions", response_model=CcSession, status_code=201, summary="새 세션")
async def create_session(project_id: str, body: SessionCreate) -> dict[str, Any]:
    s = _svc()
    _check_harness(body.harness)
    out = s.create_session(project_id, body.title, body.model, body.permission_mode, profile=body.profile, effort=body.effort,
                           harness=body.harness, engine=body.engine)
    if s.settings.get("prewarm_process"):
        out = await s.manager.open_session(out["id"])
    return out


@router.post("/conversations", response_model=CcSession, status_code=201,
             summary="새 CLI 대화 — 작업 폴더를 자동으로 만들고 세션 생성 (workspace/날짜_시각_요청)")
async def new_conversation(body: ConversationCreate, request: Request) -> dict[str, Any]:
    from server import deps

    s = _svc()
    from agent import locations

    _check_harness(body.harness)
    cfg = deps.settings_for(deps.user(request))   # 로그인 사용 모드면 그 사람의 작업 폴더 아래
    try:
        root = locations.work_base(body.base_dir, cfg)   # '' = 기준 폴더 자체
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    out = s.new_conversation(root, body.task, body.model, body.permission_mode, cfg.session_folders or bool(body.base_dir.strip()),
                             profile=body.profile, effort=body.effort, harness=body.harness, engine=body.engine)
    if s.settings.get("prewarm_process"):
        out = await s.manager.open_session(out["id"])
    return out


@router.post("/projects/{project_id}/sessions/import", response_model=CcSession, status_code=201,
             summary="터미널·VS Code 에서 만든 Claude Code 세션 가져오기")
async def import_session(project_id: str, body: SessionImport) -> dict[str, Any]:
    return _svc().import_session(project_id, body.claude_session_id)


# ───────────────────────── 세션

@router.get("/sessions", response_model=list[CcSession], summary="모든 세션 (최근 사용 순 — 사이드바 '최근' 목록용)")
async def list_sessions(limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    s = _svc()
    return [s.manager.session_out(r) for r in s.db.list_sessions()[:limit]]


@router.get("/sessions/last", response_model=CcSession | None, summary="마지막으로 쓴 세션 (화면 복구용)")
async def last_session() -> dict[str, Any] | None:
    s = _svc()
    row = s.db.last_used_session()
    return s.manager.session_out(row) if row else None


@router.get("/sessions/{session_id}", response_model=CcSession, summary="세션")
async def get_session(session_id: str) -> dict[str, Any]:
    s = _svc()
    return s.manager.session_out(s.manager._require_session(session_id))


@router.patch("/sessions/{session_id}", response_model=CcSession, summary="세션 이름·모델·권한 모드 변경")
async def update_session(session_id: str, body: SessionUpdate) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"title": body.title, "permission_mode": body.permission_mode}
    if "model" in body.model_fields_set:
        kwargs["model"] = body.model
    if "effort" in body.model_fields_set:
        kwargs["effort"] = body.effort or None
    if "profile" in body.model_fields_set:
        kwargs["profile"] = body.profile or None
    if "harness" in body.model_fields_set:
        _check_harness(body.harness or "")
        kwargs["harness"] = body.harness or None
    return await _svc().manager.update_session(session_id, **kwargs)


@router.delete("/sessions/{session_id}", response_model=Ok, summary="세션을 목록에서 삭제 (Claude Code 기록은 그대로)")
async def delete_session(session_id: str) -> Ok:
    await _svc().manager.delete_session(session_id)
    return Ok()


@router.post("/sessions/{session_id}/open", response_model=CcSession, summary="세션 열기 (최근 사용 표시 + CLI 미리 시작)")
async def open_session(session_id: str) -> dict[str, Any]:
    return await _svc().manager.open_session(session_id)


@router.get("/sessions/{session_id}/snapshot", response_model=Snapshot, summary="진행 중인 응답·승인 대기 상태")
async def snapshot(session_id: str) -> dict[str, Any]:
    s = _svc()
    s.manager._require_session(session_id)
    return s.manager.snapshot(session_id)


@router.get("/sessions/{session_id}/messages", response_model=MessagesPage, summary="대화 기록 (Claude Code 세션 기록에서, 뒤에서부터 페이지)")
async def get_messages(session_id: str, limit: int = Query(150, ge=1, le=2000),
                       before: int | None = Query(None, ge=0, description="이 위치 앞의 메시지")) -> dict[str, Any]:
    s = _svc()
    row = s.manager._require_session(session_id)
    try:
        return await asyncio.to_thread(s.manager.history_page, row, limit, before)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"세션 기록을 읽지 못했습니다: {e}") from e


@router.post("/sessions/{session_id}/messages", response_model=SendResult, summary="메시지 보내기 (응답은 이벤트로 온다)")
async def send_message(session_id: str, body: SendMessage) -> dict[str, Any]:
    return await _svc().manager.send_message(session_id, body.text, body.attachments, force=body.force)


@router.post("/sessions/{session_id}/interrupt", response_model=InterruptResult, summary="응답 중단")
async def interrupt(session_id: str) -> dict[str, Any]:
    return await _svc().manager.interrupt(session_id)


@router.post("/sessions/{session_id}/permissions/{permission_id}", response_model=Ok, summary="도구 승인·거부 / 질문 답변")
async def resolve_permission(session_id: str, permission_id: str, body: PermissionDecision) -> Ok:
    _svc().manager.resolve_permission(session_id, permission_id, body.behavior, message=body.message,
                                      apply_suggestions=body.apply_suggestions, answers=body.answers, interrupt=body.interrupt)
    return Ok()


@router.post("/sessions/{session_id}/process/start", response_model=ProcessInfo, summary="CLI 프로세스 미리 시작")
async def start_process(session_id: str, body: ProcessStartIn) -> dict[str, Any]:
    return await _svc().manager.start_process(session_id, force=body.force)


@router.post("/sessions/{session_id}/process/stop", response_model=Ok, summary="CLI 프로세스 닫기 (세션은 그대로)")
async def stop_process(session_id: str) -> Ok:
    await _svc().manager.stop_process(session_id)
    return Ok()


@router.post("/sessions/{session_id}/attachments", response_model=AttachmentOut, status_code=201, summary="파일 첨부 (base64)")
async def upload_attachment(session_id: str, body: AttachmentIn) -> dict[str, Any]:
    s = _svc()
    s.manager._require_session(session_id)
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(400, "첨부 파일 내용(base64)이 올바르지 않습니다") from e
    try:
        return s.manager.attachments.save(session_id, body.name, data, body.mime or None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ───────────────────────── 실시간 이벤트

@router.get("/events", summary="실시간 이벤트 (text/event-stream)", response_class=StreamingResponse,
            responses={200: {"description": "`data:` 마다 이벤트 JSON 하나 ({type, session_id, seq, ts, data}). "
                                            "session_id 를 주면 그 세션의 스트리밍·승인 이벤트와 시작 스냅숏도 온다",
                             "content": {"text/event-stream": {}}}})
async def events(request: Request, session_id: str | None = None) -> StreamingResponse:
    s = _svc()
    hub, manager = s.hub, s.manager
    client = hub.add_client()
    hub.send_to(client, "hello", None, {"client_id": client.id})
    if session_id:
        client.subscriptions.add(session_id)
        if s.db.get_session(session_id):
            hub.send_to(client, "snapshot", session_id, manager.snapshot(session_id))

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(client.queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                if ev.get("type") == "resync":
                    return
        finally:
            hub.remove_client(client)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


async def shutdown() -> None:
    """관리 서버 종료 때 부른다 (server/app.py lifespan)."""
    if not installed():
        return
    from agent.claude_code import service

    await service.shutdown()

