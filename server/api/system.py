"""상태 한눈에 보기, 모델 서버 켜기·끄기, 쓸 모델 바꾸기."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from agent import accounts, connections, crawler, providers
from agent.config import get_secret, managed_hosts, model_file_state
from agent.index import DocIndex
from agent.runtime import health, props
from server import deps
from server.api.common import ApiModel, ERRORS

router = APIRouter(tags=["system"], responses=ERRORS)

RuntimeState = Literal["ok", "loading", "down", "occupied"]


# ───────────────────────── 모양

class BackendInfo(ApiModel):
    url: str
    model: str
    context_length: int


class RuntimeInfo(ApiModel):
    state: RuntimeState
    port: int
    stage: str = ""
    error: str = ""
    notice: str = ""
    managed: bool = False
    exe: str
    model_file: str
    model_size_gb: float | None = None
    problems: list[str] = []
    n_ctx: int | None = None          # 아래 셋은 모델 서버가 켜져 있을 때 /props 에서 읽은 실제 값
    model_path: str | None = None
    build: Any = None
    log_tail: str | None = None


class AllowedDir(ApiModel):
    path: str
    mode: str


class WebInfo(ApiModel):
    provider: str
    regions: list[str]


class CrawlInfo(ApiModel):
    source: str
    state: str
    pages: int = 0
    changed: int = 0
    removed: int = 0
    errors: int = 0
    last_error: str = ""
    current_url: str = ""
    started_at: str = ""
    finished_at: str = ""


class SourceInfo(ApiModel):
    name: str
    description: str
    start_urls: list[str]
    prefix: str
    managed: bool
    pages: int = 0
    last_fetched: str | None = None
    crawl: CrawlInfo | None = None


class ApiInfo(ApiModel):
    name: str
    description: str
    base_url: str
    methods: list[str]
    managed: bool
    auth: bool | None = None          # None = 인증 없음, True/False = 토큰이 저장됐는지
    secret_name: str | None = None
    title: str = ""
    auth_kind: str | None = Field(default=None, description="token = 고정 토큰, login = 계정으로 로그인 (None = 인증 없음)")
    login_url: str = ""
    auth_header: str = ""
    auth_prefix: str = ""
    auth_source: str = Field(default="", description="인증 설정을 어디서 알았는지 (문서·관리 화면)")
    docs_url: str = ""
    docs: str = ""
    endpoints: int = 0


class HostInfo(ApiModel):
    rule: str
    managed: bool
    registered: bool = False   # 등록된 API·사이트라 자동 허용된 것 (지울 수 없음)


class IntranetInfo(ApiModel):
    sources: list[SourceInfo]
    apis: list[ApiInfo]
    allowed_hosts: list[HostInfo]


class ModelInfo(ApiModel):
    name: str
    note: str
    file: str
    state: Literal["ready", "downloading", "missing"]
    size_gb: float | None = None
    active: bool


class GpuInfo(ApiModel):
    name: str
    used_mb: int
    total_mb: int
    util: int


class ExternalModelInfo(ApiModel):
    """입력창 모델 선택에 보일 외부 AI 모델 (관리자가 연결·켜 둔 것). key 를 실행 시작의 model 로 보낸다."""
    key: str = Field(description="ext:<연결 id>:<모델 id>")
    connection: str
    connection_title: str
    kind: Literal["openai", "anthropic", "gemini"]
    model: str
    title: str
    context_length: int
    ready: bool = Field(description="키가 있고 마지막 연결 확인이 성공했는지")
    problem: str = Field(default="", description="마지막 연결 확인 실패 사유")


class AccessInfo(ApiModel):
    login_enabled: bool
    offline: bool = Field(description="폐쇄망 스위치 — 켜져 있으면 외부 모델을 쓰지 않는다")
    is_admin: bool


class SystemStatus(ApiModel):
    backend: BackendInfo
    runtime: RuntimeInfo
    workspace: str
    allowed_dirs: list[AllowedDir]
    limits: dict[str, Any]
    web: WebInfo
    intranet: IntranetInfo
    models: list[ModelInfo]
    external_models: list[ExternalModelInfo] = []
    access: AccessInfo
    active_run: str | None = None
    queued: int = Field(default=0, description="로컬 모델 차례를 기다리는 실행 수")
    gpu: GpuInfo | None = None


class RuntimeStateOut(ApiModel):
    state: RuntimeState


class ModelChoice(ApiModel):
    name: str


class ModelChoiceOut(ApiModel):
    model: str
    state: RuntimeState


# ───────────────────────── 상태

def _intranet_status(s) -> IntranetInfo:
    index_path = s.data_path / "index.db"
    stats: dict[str, Any] = {}
    if index_path.exists():
        idx = DocIndex(index_path)
        stats = idx.stats()
        idx.close()
    sources = []
    for src in s.intranet.sources:
        job = crawler.job_status(src.name)
        sources.append(SourceInfo(name=src.name, description=src.description, start_urls=src.start_urls,
                                  prefix=src.prefixes()[0], managed=src.managed,
                                  **stats.get(src.name, {"pages": 0, "last_fetched": None}),
                                  crawl=CrawlInfo(**job.to_dict()) if job else None))
    apis = [ApiInfo(name=a.name, description=a.description, base_url=a.base_url, methods=a.allow_methods,
                    managed=a.managed, auth=None if a.auth is None else not a.auth.missing(),
                    secret_name=(a.auth.secret or None) if a.auth else None, docs_url=a.docs_url, docs=a.docs,
                    title=a.title, auth_kind=a.auth.kind if a.auth else None,
                    login_url=a.auth.login_url if a.auth else "", auth_header=a.auth.header if a.auth else "",
                    auth_prefix=a.auth.prefix if a.auth else "", auth_source=a.auth.source if a.auth else "",
                    endpoints=sum(1 for line in a.docs.splitlines()
                                  if line.split(" ", 1)[0] in ("GET", "POST", "PUT", "PATCH", "DELETE")))
            for a in s.apis]
    managed = set(managed_hosts())
    hosts = [HostInfo(rule=r, managed=r in managed) for r in s.web.allow_private_hosts]
    hosts += [HostInfo(rule=r, managed=False, registered=True) for r in s.registered_hosts()
              if r not in s.web.allow_private_hosts]
    return IntranetInfo(sources=sources, apis=apis, allowed_hosts=hosts)


def _gpu() -> GpuInfo | None:
    if not shutil.which("nvidia-smi"):
        return None
    out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip()
    if not out:
        return None
    name, used, total, util = [x.strip() for x in out.splitlines()[0].split(",")]
    try:
        return GpuInfo(name=name, used_mb=int(used), total_mb=int(total), util=int(util))
    except ValueError:   # 일부 노트북 GPU 는 사용률을 [N/A] 로 준다
        return GpuInfo(name=name, used_mb=int(used), total_mb=int(total), util=0)


@router.get("/status", response_model=SystemStatus, summary="모델 서버·모델·외부 모델·사내 연결·GPU 상태")
def status() -> SystemStatus:
    who = deps.user()
    s = deps.view_settings(who)
    srv = deps.llm_server
    state = health(s)
    if state in ("down", "occupied") and (srv.stage or srv.running):
        state = "loading"   # 압축 해제·포트 변경 중이거나, 프로세스는 떴지만 아직 포트를 열지 않음
    model_file = s.path(s.runtime.model_path)
    runtime = RuntimeInfo(
        state=state, port=s.runtime.port, stage=srv.stage or "", error=srv.error or "", notice=srv.notice or "",
        managed=srv.running, exe=str(s.path(s.runtime.exe)), model_file=str(model_file),
        model_size_gb=round(model_file.stat().st_size / 2**30, 2) if model_file.exists() else None,
        problems=srv.problems(), **(props(s) if state == "ok" else {}),
        log_tail=srv.log_tail() if state == "down" and srv.proc is not None else None)
    active = deps.active(accounts.owner_id(who))
    access = accounts.load_access()
    return SystemStatus(
        backend=BackendInfo(url=s.backend.base_url, model=s.backend.model, context_length=s.backend.context_length),
        runtime=runtime,
        workspace=str(s.workspace_path),
        allowed_dirs=[AllowedDir(path=str(s.path(d.path)), mode=d.mode) for d in s.allowed_dirs],
        limits=s.limits.model_dump(),
        web=WebInfo(provider=s.web.search_provider, regions=s.web.regions),
        intranet=_intranet_status(s),
        models=[ModelInfo(name=m.name, note=m.note, file=str(s.path(m.file)), state=model_file_state(s.path(m.file)),
                          size_gb=round(s.path(m.file).stat().st_size / 2**30, 1) if s.path(m.file).exists() else None,
                          active=m.name == s.backend.model) for m in s.models],
        external_models=[ExternalModelInfo(**m) for m in providers.choices()],
        access=AccessInfo(login_enabled=access.login_enabled, offline=access.offline, is_admin=accounts.is_admin(who)),
        active_run=active.agent.run_id if active else None,
        queued=len(deps.local_queue.waiting),
        gpu=_gpu(),
    )


# ───────────────────────── 모델 서버

@router.post("/runtime/start", response_model=RuntimeStateOut, summary="모델 서버 켜기 (관리자)",
             dependencies=[Depends(deps.require_admin)])
def runtime_start() -> RuntimeStateOut:
    current = health(deps.load_settings())
    if current in ("ok", "loading"):
        return RuntimeStateOut(state=current)
    deps.llm_server.settings = deps.load_settings()   # 그사이 바뀐 설정(모델 선택·포트 등)을 반영
    if problems := deps.llm_server.problems():
        raise HTTPException(400, "; ".join(problems))
    deps.llm_server.start_async()
    return RuntimeStateOut(state="loading")


@router.post("/runtime/stop", response_model=RuntimeStateOut, summary="모델 서버 끄기 (이 관리 화면이 띄운 것만, 관리자)",
             dependencies=[Depends(deps.require_admin)])
def runtime_stop() -> RuntimeStateOut:
    if deps.active_local():
        raise HTTPException(409, "실행 중인 작업이 있습니다. 먼저 중지하세요.")
    if not deps.llm_server.running:
        raise HTTPException(400, "이 관리 화면이 띄운 모델 서버가 아닙니다 (직접 띄운 서버는 직접 끄세요).")
    deps.llm_server.stop()
    return RuntimeStateOut(state="down")


@router.put("/models/active", response_model=ModelChoiceOut,
            summary="쓸 로컬 모델 바꾸기 (같은 모델이면 다시 켜서 바뀐 값 적용). 적재에 수십 초. 관리자",
            dependencies=[Depends(deps.require_admin)])
def select_model(body: ModelChoice) -> ModelChoiceOut:
    if deps.active_local():
        raise HTTPException(409, "실행 중인 작업이 있습니다. 끝나거나 중지한 뒤 바꾸세요.")
    if health(deps.load_settings()) in ("ok", "loading") and not deps.llm_server.running:
        raise HTTPException(409, "직접 띄운 모델 서버라 여기서 바꿀 수 없습니다. 그 서버를 끄고 다시 시도하세요.")
    try:
        connections.set_active_model(deps.load_settings(), body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    deps.llm_server.stop()
    deps.llm_server.settings = deps.load_settings()
    deps.llm_server.start_async()
    return ModelChoiceOut(model=body.name, state="loading")
