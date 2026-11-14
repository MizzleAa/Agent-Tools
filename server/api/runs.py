"""실행: 시작 · 목록 · 상세 · 실시간 이벤트(SSE) · 승인 · 자동 승인 범위 · 중지 · 작업 폴더 열기 · 대화 삭제.

실행은 별도 스레드에서 돈다. 이벤트는 메모리(RunHandle)와 SQLite 에 쌓이고, 화면은
GET /runs/{id}/events (text/event-stream) 로 받는다. 승인이 필요하면 실행 스레드가 멈춰 기다리고
POST /runs/{id}/approvals 가 깨운다.

로그인 사용 모드: 실행·대화는 실행한 사람만 보고(관리자는 전부 본다), 승인·중지·자동 승인은 실행한 사람만 한다.
한 사람은 한 번에 하나씩 실행하고, 로컬 모델(GPU 하나)을 쓰는 실행은 온 순서대로 차례를 기다린다 (외부 모델은 바로 돈다).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import Field

from agent import accounts, apiauth, harness, locations, providers, session
from agent.accounts import User
from agent.config import AllowedDir
from agent.events import Error, Final, Note, RunStarted
from agent.loop import history_from_events
from agent.profiles import load_profile
from agent.workflow import load_workflow, resume_state
from server import deps
from server.api.common import ApiModel, ERRORS, Ok
from server.api.events import RunEvent

router = APIRouter(tags=["runs"], responses=ERRORS)


# ───────────────────────── 모양

class RunCreate(ApiModel):
    task: str = Field(description="요청 (사용자 메시지)")
    profile: str = Field(default="general", description="용도(프로필) 이름. workflow 를 주면 무시")
    harness: str = Field(default="", description="붙일 하네스 (회사·프로젝트 하네스 이름, GET /harnesses). '' = 공통만. "
                                                "회사 하네스는 그 회사 사람에게 저절로 붙는다. 워크플로는 노드에 정한 하네스가 먼저")
    workflow: str = Field(default="", description="주면 프로필 대신 이 워크플로를 실행")
    resume: bool = Field(default=False, description="워크플로 '멈춘 노드부터 계속': 이전 실행에서 성공한 노드는 다시 돌리지 않음")
    auto_approve: deps.AutoMode = "none"
    max_steps: int | None = Field(default=None, ge=0, description="0 = 무제한(시간 제한도 풀림), 비우면 설정값")
    parent_run_id: str = Field(default="", description="이어서 하기: 이 실행의 대화를 이어받는다")
    model: str = Field(default="", description="'' = 이 서버의 로컬 모델(지금 고른 것) · 'ext:<연결>:<모델>' = 외부 AI 모델 "
                                               "(GET /status 의 external_models[].key)")
    base_dir: str = Field(default="", description="작업 위치 (관리자만): 기준 폴더(환경변수 AGENT_WORK_ROOT, 없으면 작업 폴더 workspace) "
                                                 "아래의 상대 경로. 그 아래에 '날짜_시각_요청' 폴더를 만든다 (없는 폴더는 만든다). "
                                                 "이어서 하기는 이전 폴더를 쓴다")
    effort: Literal["", "low", "medium", "high", "xhigh", "max"] = Field(
        default="", description="추론 강도. '' = 기본(외부 모델은 관리자가 연결에 정한 값, 로컬 모델은 용도 설정). "
                                "외부: OpenAI·Gemini 는 xhigh·max 를 high 로. 로컬: low = 생각 끄기, 그 밖 = 생각 켜기")


class RunRef(ApiModel):
    run_id: str


class RunSummary(ApiModel):
    id: str
    task: str
    profile: str
    model: str = ""
    status: str = Field(default="running", description="running · done · partial · failed · no_action · max_steps · "
                                                       "timeout · loop · cancelled · error · interrupted")
    answer: str = ""
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    created_at: str
    finished_at: str | None = None
    parent_id: str = ""
    workdir: str = ""
    user_id: str = Field(default="", description="실행한 사람 ('' = 로그인 없이 혼자 쓰기)")
    company_id: str = ""
    provider: str = Field(default="", description="'' 또는 local = 로컬 모델 · openai · anthropic · gemini")
    connection: str = Field(default="", description="외부 모델 연결 id")


class RunDetail(RunSummary):
    events: list[RunEvent]


class ApprovalDecision(ApiModel):
    call_id: str
    approved: bool


class AutoApprove(ApiModel):
    mode: deps.AutoMode


class FolderPath(ApiModel):
    path: str


class Deleted(ApiModel):
    deleted: int


# ───────────────────────── 실행 시작

def _worker(handle: deps.RunHandle, task: str, history: list[dict[str, Any]] | None, parent_id: str,
            who: User | None = None, tags: dict[str, str] | None = None) -> None:
    accounts.CURRENT.set(who)   # 도구가 '누구의 일인지' 안다 (사내 API 본인 계정 · 공유 위치 허용 · 관리 도구 제한)
    run_id = handle.agent.run_id
    seq = 0

    def emit(ev: Any) -> None:
        nonlocal seq
        deps.store.record(run_id, seq, ev)
        handle.events.append({"seq": seq, **ev.to_dict()})
        seq += 1

    try:
        for ev in handle.agent.run(task, history, parent_id):
            emit(ev)
            if isinstance(ev, RunStarted):
                deps.store.tag(run_id, **(tags or {}))
                if handle.local:   # 로컬 모델은 한 번에 한 실행씩 — 앞 실행이 끝날 때까지 기다린다 (중지하면 바로 끝)
                    deps.local_queue.acquire(run_id, handle.agent.cancel, lambda ahead: emit(Note(
                        0, "queue", f"다른 작업이 로컬 모델을 쓰고 있어 차례를 기다립니다 (앞에 {ahead}개)", "")))
    except Exception as e:  # 예상 못한 오류도 화면과 기록에 남긴다
        for ev in (Error(f"{type(e).__name__}: {e}"), Final("error", str(e), 0, 0, 0, 0.0)):
            emit(ev)
    finally:
        if handle.local:
            deps.local_queue.release(run_id)
        handle.done = True


def _session_dir(s, task: str, parent: dict[str, Any] | None) -> Path | None:
    """대화(세션) 전용 폴더. 이어서 하는 실행은 같은 폴더를 쓰고, 새 대화면 새로 만든다."""
    if not s.session_folders:
        return None
    if parent is not None:
        if parent.get("workdir") and Path(parent["workdir"]).is_dir():
            return Path(parent["workdir"])
        root = parent   # 예전 기록(폴더 없음)을 이어서 하면, 대화 첫 요청 이름으로 폴더를 만든다
        while root.get("parent_id") and (up := deps.store.get_run(root["parent_id"])):
            root = up
        task = root["task"]
    return session.create_session_dir(s.workspace_path, task)


def _chain(run: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    """같은 대화의 이전 실행들 (가까운 것부터)."""
    out = [run]
    while len(out) < limit and out[-1].get("parent_id") and (up := deps.store.get_run(out[-1]["parent_id"])):
        out.append(up)
    return out


def _rebase(s, body: RunCreate, parent: dict[str, Any] | None, who: User | None):
    """작업 위치: 관리자가 고른 위치(기준 폴더 아래 상대 경로)를 이 실행의 작업 폴더(workspace)로 삼고 쓰기를 허용한다
    (이 실행에만 — 설정 파일은 그대로). 기준 폴더를 환경변수(AGENT_WORK_ROOT)로 정했으면 관리자의 새 작업은 고르지 않아도 그 아래.
    이어서 하기는 이전 실행 폴더의 상위를 다시 작업 위치로 쓴다 (작업목록.md 도 같은 곳에)."""
    base: Path | None = None
    if parent is None:
        if body.base_dir.strip() and not accounts.is_admin(who):
            raise HTTPException(403, "작업 위치는 관리자만 고를 수 있습니다")
        if accounts.is_admin(who) and (body.base_dir.strip() or os.environ.get(locations.WORK_ROOT_ENV, "").strip()):
            try:
                base = locations.work_base(body.base_dir, s)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
    elif parent.get("workdir") and Path(parent["workdir"]).is_dir() \
            and locations.norm(Path(parent["workdir"]).parent) != locations.norm(s.workspace_path):
        base = Path(parent["workdir"]).parent
    if base is None:
        return s
    s.workspace = str(base)
    if not any(locations.norm(s.path(d.path)) == locations.norm(base) for d in s.allowed_dirs):
        s.allowed_dirs = [*s.allowed_dirs, AllowedDir(path=str(base), mode="rw")]
    return s


def _workflow_runner(s, body: RunCreate, parent: dict[str, Any] | None, handle: deps.RunHandle,
                     limits: dict[str, Any]):
    """워크플로 실행. 같은 대화의 이전 답은 {{previous}} 로, '계속'이면 이전에 성공한 노드의 결과를 그대로 쓴다."""
    wf = load_workflow(body.workflow)
    resume, original, note = {}, "", ""
    if body.resume and parent is not None and any(e.get("type") == "WorkflowStarted" and e.get("name") == wf.name
                                                    for e in parent["events"]):
        resume, original, note = resume_state(parent["events"], wf)
    return deps.WorkflowRunner(s, wf, approver=lambda req: handle.approver(req), workdir=_session_dir(s, body.task, parent),
                               previous=(parent or {}).get("answer") or "", resume=resume, resume_note=note,
                               input_text=original, **limits)


def _check_allowed(who: User | None, body: RunCreate, s) -> None:
    """회사에 허용된 용도(프로필)만 (관리 › 사용자·회사). 워크플로는 노드의 용도가 모두 허용돼야 한다.
    회사·프로젝트 하네스의 용도는 그 하네스를 쓸 수 있으면 허용 (하네스의 대상 회사로 정한다)."""
    if body.harness and not harness.usable(body.harness, s.audience):
        raise HTTPException(404 if harness.get_scope(body.harness) is None else 403, f"쓸 수 없는 하네스입니다: {body.harness}")
    allowed = deps.allowed_profiles(who)
    if allowed is None:
        return
    allowed = allowed | {n for sc in harness.active(s.audience, body.harness) if sc for n in harness.role_names(sc)}
    if body.workflow:
        try:
            wf = load_workflow(body.workflow)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        names = {n["profile"] for n in wf.summary()}
        allowed |= {n for node in wf.nodes if node.harness and harness.usable(node.harness, s.audience)
                    for n in harness.role_names(node.harness)}
        if names - allowed:
            raise HTTPException(403, f"이 워크플로에는 회사에 허용되지 않은 용도가 있습니다: {', '.join(sorted(names - allowed))}")
    elif body.profile not in allowed:
        raise HTTPException(403, f"'{body.profile}' 용도는 회사에 허용되지 않았습니다. 쓸 수 있는 용도: {', '.join(sorted(allowed))}")


@router.post("/runs", response_model=RunRef, summary="실행 시작 (용도 또는 워크플로, 로컬 또는 외부 모델)")
def start_run(body: RunCreate, request: Request) -> RunRef:
    who = deps.user(request)
    owner = accounts.owner_id(who)
    if not body.task.strip():
        raise HTTPException(400, "작업 내용을 입력하세요")
    if deps.active(owner):
        raise HTTPException(409, "이미 실행 중인 작업이 있습니다. 끝나거나 중지한 뒤 다시 시도하세요.")
    _check_allowed(who, body, deps.settings_for(who))
    from server.app import _prune_images
    _prune_images()   # 이미지 원본 보관 날수 (실행을 시작할 때마다 지난 것을 지운다)
    task, hidden = apiauth.mask_credentials(body.task)   # API-4: 채팅에 적은 비밀번호·토큰은 모델·기록에 넘기지 않는다
    if hidden:
        body = body.model_copy(update={"task": f"{task}\n\n{apiauth.MASK_NOTE}"})
    history, parent = None, None
    if body.parent_run_id:
        parent = deps.store.get_run(body.parent_run_id)
        if parent is None or parent.get("user_id", "") != owner:   # 남의 대화는 이어서 할 수 없다
            raise HTTPException(404, "이어서 할 이전 실행을 찾을 수 없습니다")
        history = history_from_events(parent["events"])
    s = _rebase(deps.settings_for(who), body, parent, who)
    s.harness = body.harness
    external = providers.is_external(body.model)
    if external:
        try:
            s.backend = providers.resolve(body.model)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if body.effort:
            s.backend.effort = providers.effort_for(s.backend.provider, body.effort)
    elif body.effort:
        s.think = providers.local_think(body.effort)
    handle = deps.RunHandle(agent=None, auto=body.auto_approve, owner=owner, local=not external)
    limits = dict(max_steps=body.max_steps, max_seconds=0 if body.max_steps == 0 else None)
    try:
        if body.workflow:
            handle.agent = _workflow_runner(s, body, parent, handle, limits)
        else:
            handle.agent = deps.Agent(s, load_profile(body.profile, scopes=harness.active(s.audience, s.harness)),
                                      approver=lambda req: handle.approver(req),
                                      workdir=_session_dir(s, body.task, parent), **limits)
            if parent is not None and hasattr(handle.agent, "known_errors"):   # E6: 이전 실행에서 고친 '없는 이름'을 넘긴다
                for run in _chain(parent):
                    handle.agent.known_errors.seed(run["events"])
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    deps.runs[handle.agent.run_id] = handle
    tags = {"user_id": owner, "company_id": who.company_id if who else "",
            "provider": s.backend.provider if external else "local", "connection": s.backend.connection}
    threading.Thread(target=_worker, args=(handle, body.task, history, body.parent_run_id, who, tags), daemon=True).start()
    return RunRef(run_id=handle.agent.run_id)


# ───────────────────────── 조회

def _visible(run_id: str, who: User | None) -> None:
    """본인 실행이거나 관리자 — 아니면 '없음'과 같은 404 (남의 실행이 있는지도 알 수 없게)."""
    if not deps.can_see(deps.run_owner(run_id), who):
        raise HTTPException(404, "기록이 없습니다")


@router.get("/runs", response_model=list[RunSummary], summary="최근 실행 목록 (로그인 사용: 내 것만)")
def list_runs(request: Request, limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    who = deps.user(request)
    return deps.store.list_runs(limit, user_id=None if who is None else who.id)


@router.get("/threads/{root_id}", response_model=list[RunSummary], summary="대화 하나의 실행들 (처음 것부터, 이어서 한 것 포함)")
def get_thread(root_id: str, request: Request) -> list[dict[str, Any]]:
    _visible(root_id, deps.user(request))
    ids = set(deps.store.thread_ids(root_id))
    runs = [r for r in deps.store.list_runs(100_000, user_id=deps.store.owner(root_id)) if r["id"] in ids]
    return sorted(runs, key=lambda r: (r["created_at"], r["id"]))


@router.get("/runs/{run_id}", response_model=RunDetail, summary="실행 하나 (이벤트 전부 포함)")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    _visible(run_id, deps.user(request))
    run = deps.store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "기록이 없습니다")
    run["events"] = [{"seq": i, **ev} for i, ev in enumerate(run["events"])]
    return run


@router.get("/runs/{run_id}/events", summary="실시간 이벤트 (text/event-stream)",
            response_class=StreamingResponse,
            responses={200: {"description": "`data:` 마다 RunEvent JSON 하나. 끝나면 `event: end`",
                             "content": {"text/event-stream": {}}}})
async def stream(run_id: str, request: Request, since: int = Query(0, ge=0)) -> StreamingResponse:
    _visible(run_id, deps.user(request))
    handle = deps.runs.get(run_id)

    async def live():
        i = since
        while True:
            while i < len(handle.events):
                ev = handle.events[i]
                yield f"id: {ev['seq']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                i += 1
            if handle.done and i >= len(handle.events):
                yield "event: end\ndata: {}\n\n"
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.15)

    async def replay():   # 서버를 다시 켠 뒤 등 메모리에 없는 실행은 저장된 기록을 한 번에 흘려보낸다
        run = deps.store.get_run(run_id)
        if run is None:
            yield "event: end\ndata: {}\n\n"
            return
        for seq, ev in enumerate(run["events"]):
            if seq >= since:
                yield f"id: {seq}\ndata: {json.dumps({'seq': seq, **ev}, ensure_ascii=False)}\n\n"
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(live() if handle else replay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# ───────────────────────── 실행 중 조작

def _live(run_id: str, request: Request) -> deps.RunHandle:
    """실행 중이고 내가 시작한 실행 (승인·중지·자동 승인은 실행한 사람만 — 관리자도 남의 실행은 못 바꾼다)."""
    handle = deps.runs.get(run_id)
    if not handle or handle.done or handle.owner != accounts.owner_id(deps.user(request)):
        raise HTTPException(404, "실행 중인 작업이 아닙니다")
    return handle


@router.post("/runs/{run_id}/approvals", response_model=Ok, summary="승인 대기 중인 도구 호출에 답하기 (실행한 사람만)")
def approve(run_id: str, body: ApprovalDecision, request: Request) -> Ok:
    handle = deps.runs.get(run_id)
    if not handle or handle.owner != accounts.owner_id(deps.user(request)) or not handle.resolve(body.call_id, body.approved):
        raise HTTPException(404, "대기 중인 승인 요청이 없습니다")
    return Ok()


@router.put("/runs/{run_id}/auto-approve", response_model=AutoApprove, summary="실행 도중 자동 승인 범위 바꾸기 (실행한 사람만)")
def set_auto(run_id: str, body: AutoApprove, request: Request) -> AutoApprove:
    handle = _live(run_id, request)
    handle.auto = body.mode
    return AutoApprove(mode=handle.auto)


@router.post("/runs/{run_id}/cancel", response_model=Ok, summary="실행 중지 (실행한 사람만)")
def cancel(run_id: str, request: Request) -> Ok:
    _live(run_id, request).agent.cancel.set()
    return Ok()


@router.post("/runs/{run_id}/open-folder", response_model=FolderPath, summary="이 대화의 작업 폴더를 탐색기로 열기 (서버 PC 에서만)")
def open_folder(run_id: str, request: Request) -> FolderPath:
    _visible(run_id, deps.user(request))
    run = deps.store.get_run(run_id)
    if run is None or not run.get("workdir"):
        raise HTTPException(404, "이 실행에는 작업 폴더가 없습니다")
    if (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(400, "탐색기는 서버 PC 화면에서만 열 수 있습니다. 다른 PC 에서는 '작업 폴더 · 코드'에서 파일을 보세요.")
    folder = Path(os.path.realpath(run["workdir"]))
    in_workspace = folder.is_relative_to(Path(os.path.realpath(deps.load_settings().workspace_path)))
    if not folder.is_dir() or not (in_workspace or accounts.is_admin(deps.user(request))):   # 다른 작업 위치는 관리자가 고른 것
        raise HTTPException(404, "작업 폴더를 찾을 수 없습니다")
    if hasattr(os, "startfile"):
        os.startfile(folder)   # Windows 탐색기 (관리 화면과 같은 PC)
    return FolderPath(path=str(folder))


IMAGE_HEADERS = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"}


@router.get("/runs/{run_id}/images/{image_id}/{size}", include_in_schema=False)
def run_image(run_id: str, image_id: int, size: Literal["thumb", "view", "original"], request: Request) -> FileResponse:
    """실행이 사내 API 에서 받아 둔 이미지 (api_get · show_images). 화면의 <img src> 가 부른다 — 주소에 인증 정보가 없고,
    사내 API 에 다시 묻지 않는다 (받아 둔 파일만). thumb: 작은 그림 · view: 크게 보기용(긴 변 1600) · original: 받은 그대로.
    원본은 보관 날수(설정 '이미지 보관')가 지나면 지워지고, 그 뒤 view·original 은 410 (썸네일은 남는다).
    (이미지 바이트라 JSON 계약(OpenAPI)에는 넣지 않는다 — 화면 파일과 같은 정적 응답.)"""
    from agent import imaging
    from agent.image_store import VIEW_SIDE, image_dir
    if not re.fullmatch(r"[\w-]+", run_id) or not 1 <= image_id <= 9999:
        raise HTTPException(404, "이미지가 없습니다")
    if not deps.can_see(deps.run_owner(run_id), deps.user(request)):
        raise HTTPException(404, "이미지가 없습니다")
    settings = deps.load_settings()
    folder = image_dir(settings.data_path / "runs" / run_id)
    orig = next(folder.glob(f"{image_id}.orig.*"), None)
    thumb = next(iter(sorted(folder.glob(f"{image_id}.thumb.*")) + sorted(folder.glob(f"{image_id}.small.*"))), None)
    if orig is None and thumb is None:
        raise HTTPException(404, "이미지가 없습니다 (받지 못했거나 지운 대화)")
    if size == "thumb":
        target = thumb or orig
    elif orig is None:   # 보관 날수가 지나 원본을 지웠다
        raise HTTPException(410, f"원본은 보관 기간({settings.images.keep_days}일)이 지나 지웠습니다 — 썸네일만 남아 있습니다")
    elif size == "view":
        fmt = orig.suffix[1:]
        view = folder / f"{image_id}.view.png"
        if not view.is_file():
            data = orig.read_bytes()
            w, h = imaging.size_of(data, fmt) or (0, 0)
            if fmt not in imaging.BROWSER or max(w, h) > VIEW_SIDE:   # 큰 원본(BMP 등)은 줄여서 보여 준다
                if (png := imaging.shrink_to_png(data, fmt, VIEW_SIDE)) is not None:
                    tmp = view.with_suffix(".tmp")
                    tmp.write_bytes(png)
                    os.replace(tmp, view)
        target = view if view.is_file() else (thumb if fmt not in imaging.BROWSER and thumb else orig)
    else:
        target = orig if orig.suffix[1:] in imaging.BROWSER or thumb is None else thumb   # 브라우저가 못 그리는 원본은 바꾼 그림으로
    ext = target.suffix[1:].lower()
    media = "image/png" if ext == "png" else imaging.RASTER.get(ext, "application/octet-stream")
    return FileResponse(target, media_type=media, headers=IMAGE_HEADERS)


@router.delete("/threads/{root_id}", response_model=Deleted, summary="대화 삭제 (이어서 한 실행·저장된 도구 출력까지)")
def delete_thread(root_id: str, request: Request) -> Deleted:
    _visible(root_id, deps.user(request))
    ids = deps.store.thread_ids(root_id)
    if not ids:
        raise HTTPException(404, "기록이 없습니다")
    if any(rid in deps.runs and not deps.runs[rid].done for rid in ids):
        raise HTTPException(409, "실행 중인 대화는 지울 수 없습니다. 먼저 중지하세요.")
    for rid in ids:
        deps.runs.pop(rid, None)
        if re.fullmatch(r"[\w-]+", rid):   # 실행 id 로 폴더 경로를 만들므로 형식을 확인한다
            shutil.rmtree(deps.load_settings().data_path / "runs" / rid, ignore_errors=True)
    return Deleted(deleted=deps.store.delete_runs(ids))
