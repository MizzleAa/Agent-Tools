"""관리자 화면 '관리': 사용자·회사 · 비밀번호 재설정 요청 · 외부 AI 모델 연결 · 기록·사용량 · 접속 설정.

모두 관리자만 부를 수 있다 (혼자 쓰기에서는 이 PC 사람 = 관리자). 비밀번호·API 키는 응답에 나가지 않는다 —
계정 발급·비밀번호 재설정 때 만든 임시 비밀번호만 그 응답에 한 번 보인다 (관리자가 그 사람에게 전한다).
"""

from __future__ import annotations

import socket
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from agent import accounts, claude_login, locations, providers
from server import deps
from server.api.auth import UserInfo, from_server_pc, user_info
from server.api.common import ApiModel, ERRORS, Ok
from server.api.runs import RunSummary

router = APIRouter(prefix="/admin", tags=["admin"], responses=ERRORS, dependencies=[Depends(deps.require_admin)])


# ───────────────────────── 사용자 · 회사

class CompanyInfo(ApiModel):
    id: str
    name: str
    note: str
    seats: int | None = Field(default=None, description="허용(요청받은) 계정 수. null = 제한 없음")
    issued: int = Field(description="발급한 계정 수")
    profiles: list[str] = Field(description="쓸 수 있는 용도(프로필). 비면 전부")
    created_at: str


class CompanyWrite(ApiModel):
    name: str
    note: str = ""
    seats: int | None = Field(default=None, ge=0)
    profiles: list[str] = Field(default_factory=list)


class UserCreate(ApiModel):
    username: str
    display_name: str = ""
    company_id: str = ""
    role: Literal["admin", "user"] = "user"


class UserUpdate(ApiModel):
    display_name: str | None = None
    company_id: str | None = None
    role: Literal["admin", "user"] | None = None
    active: bool | None = None


class IssuedAccount(ApiModel):
    user: UserInfo
    temp_password: str = Field(description="임시 비밀번호 — 이 응답에만 한 번 보인다. 첫 로그인 때 바꿔야 한다")


class ResetRequestInfo(ApiModel):
    id: str
    username: str
    company: str
    note: str
    user_id: str = Field(description="그 아이디의 사용자 (없는 아이디면 빈 값)")
    status: str = Field(description="pending · done · dismissed")
    created_at: str
    handled_at: str
    handled_by: str


def _company(c: accounts.Company) -> CompanyInfo:
    return CompanyInfo(id=c.id, name=c.name, note=c.note, seats=c.seats, issued=deps.accounts.seat_count(c.id),
                       profiles=c.profiles, created_at=c.created_at)


def _who(request: Request) -> str:
    user = deps.user(request)
    return user.username if user else "(이 PC)"


@router.get("/companies", response_model=list[CompanyInfo], summary="회사 목록")
def companies() -> list[CompanyInfo]:
    return [_company(c) for c in deps.accounts.companies()]


@router.post("/companies", response_model=CompanyInfo, summary="회사 만들기")
def create_company(body: CompanyWrite) -> CompanyInfo:
    try:
        return _company(deps.accounts.save_company(body.name, body.note, body.seats, body.profiles))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/companies/{company_id}", response_model=CompanyInfo, summary="회사 고치기 (이름 · 메모 · 계정 수 · 쓸 수 있는 용도)")
def update_company(company_id: str, body: CompanyWrite) -> CompanyInfo:
    try:
        return _company(deps.accounts.save_company(body.name, body.note, body.seats, body.profiles, company_id))
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/companies/{company_id}", response_model=Ok, summary="회사 지우기 (계정이 남아 있으면 안 된다)")
def delete_company(company_id: str) -> Ok:
    try:
        deps.accounts.delete_company(company_id)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Ok()


@router.get("/users", response_model=list[UserInfo], summary="사용자 목록")
def users() -> list[UserInfo]:
    days = accounts.load_access().password_max_age_days
    return [user_info(u, days) for u in deps.accounts.users()]


@router.post("/users", response_model=IssuedAccount, summary="계정 발급 (임시 비밀번호를 만든다)")
def create_user(body: UserCreate) -> IssuedAccount:
    try:
        user, temp = deps.accounts.create_user(body.username, display_name=body.display_name, company_id=body.company_id,
                                               role=body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return IssuedAccount(user=user_info(user), temp_password=temp)


@router.put("/users/{user_id}", response_model=UserInfo, summary="사용자 고치기 (이름 · 회사 · 역할 · 사용 중지)")
def update_user(user_id: str, body: UserUpdate) -> UserInfo:
    try:
        return user_info(deps.accounts.update_user(user_id, **body.model_dump()))
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/users/{user_id}", response_model=Ok, summary="사용자 지우기 (그 사람의 기록·작업 폴더는 남는다)")
def delete_user(user_id: str) -> Ok:
    try:
        deps.accounts.delete_user(user_id)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    except ValueError as e:
        raise HTTPException(400, str(e))
    accounts.drop_user_secrets(user_id)
    return Ok()


@router.post("/users/{user_id}/reset-password", response_model=IssuedAccount,
             summary="비밀번호 재설정 — 임시 비밀번호를 만든다 (그 사람의 로그인은 끝나고, 대기 중인 요청은 처리됨)")
def reset_password(user_id: str, request: Request) -> IssuedAccount:
    try:
        temp = deps.accounts.reset_password(user_id, handled_by=_who(request))
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    return IssuedAccount(user=user_info(deps.accounts.user(user_id)), temp_password=temp)  # type: ignore[arg-type]


@router.get("/reset-requests", response_model=list[ResetRequestInfo], summary="비밀번호 재설정 요청 (최근 것부터)")
def reset_requests(pending: bool = Query(False, description="참이면 대기 중인 것만")) -> list[ResetRequestInfo]:
    return [ResetRequestInfo(**vars(r)) for r in deps.accounts.reset_requests(pending_only=pending)]


@router.post("/reset-requests/{request_id}/dismiss", response_model=Ok, summary="재설정 요청 닫기 (처리하지 않고)")
def dismiss_request(request_id: str, request: Request) -> Ok:
    try:
        deps.accounts.close_request(request_id, _who(request))
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    return Ok()


# ───────────────────────── 접속 설정

class AccessSettings(ApiModel):
    app_name: str = Field(min_length=1, max_length=40, description="화면에 보이는 서비스 이름")
    login_enabled: bool
    public_hosts: list[str] = Field(description="다른 PC 가 이 서버에 들어올 때 쓰는 주소 (IP·도메인, 포트 없이)")
    password_max_age_days: int = Field(ge=0, le=3650, description="이 날수가 지나면 비밀번호를 바꾸라고 안내 (0 = 안 함)")
    session_hours: int = Field(ge=1, le=720, description="쓰지 않고 이 시간이 지나면 다시 로그인")
    offline: bool = Field(description="폐쇄망 스위치 — 켜면 외부 AI 모델을 모두 끈다")


class AccessView(AccessSettings):
    server_addresses: list[str] = Field(description="이 서버 PC 의 주소 (접속 주소로 고를 수 있는 것)")
    port: int


def _addresses() -> list[str]:
    names = {socket.gethostname()}
    try:
        names |= set(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(n for n in names if n and not n.startswith("127."))


def _access_view(request: Request) -> AccessView:
    a = accounts.load_access()
    return AccessView(**a.model_dump(), server_addresses=_addresses(), port=request.url.port or 8765)


@router.get("/access", response_model=AccessView, summary="접속 설정 (로그인 사용 · 접속 주소 · 비밀번호 기간 · 폐쇄망 스위치)")
def get_access(request: Request) -> AccessView:
    return _access_view(request)


@router.put("/access", response_model=AccessView, summary="접속 설정 저장 (바로 적용)")
def put_access(body: AccessSettings, request: Request) -> AccessView:
    current = accounts.load_access()
    if body.login_enabled and not current.login_enabled:
        raise HTTPException(400, "로그인 사용은 '로그인 사용 켜기'(첫 관리자 만들기)로 켭니다")
    hosts = []
    for h in body.public_hosts:
        h = h.strip().lower().removeprefix("http://").removeprefix("https://").split("/")[0].rsplit(":", 1)[0].strip("[]")
        if h and h not in hosts and h not in deps.ALLOWED_HOSTS:
            hosts.append(h)
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in deps.ALLOWED_HOSTS and host not in hosts:
        raise HTTPException(400, f"지금 접속한 주소({host})를 빼면 이 화면이 바로 끊깁니다. 서버 PC 에서 바꾸거나 그 주소를 남기세요.")
    accounts.save_access(accounts.Access(app_name=body.app_name.strip() or "AI Agent", login_enabled=body.login_enabled,
                                         public_hosts=hosts,
                                         password_max_age_days=body.password_max_age_days,
                                         session_hours=body.session_hours, offline=body.offline))
    return _access_view(request)


# ───────────────────────── 외부 AI 모델 연결

class ProviderModelInfo(ApiModel):
    id: str
    title: str
    context_length: int
    enabled: bool


class ProviderInfo(ApiModel):
    id: str
    title: str
    kind: Literal["openai", "anthropic", "gemini"]
    auth: Literal["api_key", "account_login"]
    base_url: str = Field(description="비면 공급사 기본 주소")
    default_url: str
    effort: str
    enabled: bool
    key_hint: str = Field(description="저장한 키의 끝 4자리 (키 값은 돌려주지 않는다), 계정 로그인이면 프로필")
    profile: str = Field(default="", description="계정 로그인(Claude) 프로필 이름")
    login_command: str = Field(default="", description="계정 로그인: 서버 PC 에서 한 번 실행할 명령 (브라우저가 열린다)")
    models: list[ProviderModelInfo]
    checked_at: str
    check_error: str


class ProviderCreate(ApiModel):
    kind: Literal["openai", "anthropic", "gemini"]
    title: str = ""
    auth: Literal["api_key", "account_login"] = "api_key"
    api_key: str = ""
    base_url: str = ""
    effort: str = ""
    profile: str = Field(default="", description="계정 로그인(Claude): ant auth login --profile 이름 (비우면 agent)")


class ProviderModelWrite(ApiModel):
    id: str
    enabled: bool | None = None
    context_length: int | None = Field(default=None, ge=2048, le=10_000_000)


class ProviderUpdate(ApiModel):
    title: str | None = None
    api_key: str | None = Field(default=None, description="주면 키를 바꾸고 연결을 다시 확인한다")
    base_url: str | None = None
    effort: str | None = None
    enabled: bool | None = None
    models: list[ProviderModelWrite] | None = None


class ProviderKind(ApiModel):
    kind: Literal["openai", "anthropic", "gemini"]
    title: str
    default_url: str
    key_page: str = Field(description="회사 API 키를 발급받는 곳")
    account_login: bool = Field(description="회사 계정 로그인으로 연결할 수 있는지")
    account_login_note: str = Field(description="계정 로그인 안내 (안 되면 그 이유)")


class ProviderCatalog(ApiModel):
    kinds: list[ProviderKind]
    login_tool_ready: bool = Field(description="계정 로그인 도구(Anthropic 공식 CLI, runtime/ant/ant.exe)가 있는지")
    login_command_example: str = Field(description="계정 로그인 명령 예 (프로필 agent)")
    items: list[ProviderInfo]


KEY_PAGES = {"openai": "https://platform.openai.com/api-keys", "anthropic": "https://platform.claude.com/settings/keys",
             "gemini": "https://aistudio.google.com/apikey"}


def _provider(p: providers.Provider) -> ProviderInfo:
    return ProviderInfo(id=p.id, title=p.title, kind=p.kind, auth=p.auth, base_url=p.base_url,
                        default_url=providers.KINDS[p.kind]["base_url"], effort=p.effort, enabled=p.enabled,
                        key_hint=p.key_hint(), profile=p.profile,
                        login_command=providers.login_command(p) if p.auth == "account_login" else "",
                        models=[ProviderModelInfo(**m.model_dump()) for m in p.models],
                        checked_at=p.checked_at, check_error=p.check_error)


@router.get("/providers", response_model=ProviderCatalog, summary="외부 AI 모델 연결 목록 (키 값은 없음)")
def list_providers() -> ProviderCatalog:
    example = providers.Provider(id="x", kind="anthropic", auth="account_login", profile="agent")
    return ProviderCatalog(
        kinds=[ProviderKind(kind=k, title=v["title"], default_url=v["base_url"], key_page=KEY_PAGES[k],  # type: ignore[arg-type]
                            account_login=k == "anthropic", account_login_note=providers.ACCOUNT_LOGIN[k])
               for k, v in providers.KINDS.items()],
        login_tool_ready=claude_login.tool_ready(),
        login_command_example=providers.login_command(example),
        items=[_provider(p) for p in providers.all_providers()])


@router.post("/providers", response_model=ProviderInfo, summary="외부 AI 모델 연결 추가 (넣고 바로 연결 확인)")
def add_provider(body: ProviderCreate) -> ProviderInfo:
    try:
        return _provider(providers.add(body.kind, body.title, body.api_key, auth=body.auth, base_url=body.base_url,
                                       effort=body.effort, profile=body.profile))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/providers/{provider_id}", response_model=ProviderInfo, summary="연결 고치기 (이름 · 키 · 주소 · 추론 강도 · 켜기 · 모델별 켜기·길이)")
def update_provider(provider_id: str, body: ProviderUpdate) -> ProviderInfo:
    try:
        return _provider(providers.update(provider_id, title=body.title, api_key=body.api_key, base_url=body.base_url,
                                          effort=body.effort, enabled=body.enabled,
                                          models=None if body.models is None else
                                          [m.model_dump(exclude_none=True) for m in body.models]))
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/providers/{provider_id}/check", response_model=ProviderInfo, summary="연결 확인 (모델 목록을 다시 받는다)")
def check_provider(provider_id: str) -> ProviderInfo:
    try:
        return _provider(providers.refresh(provider_id))
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))


# ───────────────────────── Claude 회사 계정 로그인 (화면에서 — 서버가 공식 CLI 를 대신 띄운다)

class LoginState(ApiModel):
    provider_id: str
    mode: Literal["browser", "manual"] = Field(description="browser = 서버 PC 에 브라우저가 열림 · manual = 주소를 열고 코드를 붙여 넣음")
    state: Literal["idle", "running", "done", "failed", "cancelled"] = Field(description="idle = 아직 로그인을 시작하지 않음")
    message: str
    urls: list[str] = Field(description="로그인 주소 (manual 일 때 이것을 연다)")
    output: list[str] = Field(description="로그인 도구 출력 마지막 줄들 (문제를 찾을 때)")
    seconds: int


class LoginCode(ApiModel):
    code: str = Field(description="브라우저에서 로그인한 뒤 받은 코드 (기록에 남기지 않는다)")


def _login_provider(provider_id: str) -> providers.Provider:
    try:
        p = providers.get(provider_id)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    if p.kind != "anthropic" or p.auth != "account_login":
        raise HTTPException(400, "회사 계정 로그인으로 만든 Claude 연결만 로그인할 수 있습니다")
    return p


@router.post("/providers/{provider_id}/login", response_model=LoginState,
             summary="Claude 계정으로 로그인 시작 (서버 PC 에서 누르면 그 PC 에 브라우저, 다른 PC 면 주소+코드)")
def start_login(provider_id: str, request: Request) -> LoginState:
    p = _login_provider(provider_id)
    if not claude_login.tool_ready():
        raise HTTPException(400, f"로그인 도구가 없습니다 ({providers.ANT_EXE} — Anthropic 공식 CLI)")
    return LoginState(**claude_login.start(p.id, p.profile, manual=not from_server_pc(request),
                                           on_done=lambda: providers.refresh(p.id)))


@router.get("/providers/{provider_id}/login", response_model=LoginState, summary="로그인 진행 상황 (시작 전이면 idle)")
def login_status(provider_id: str) -> LoginState:
    if (s := claude_login.status(provider_id)) is None:
        return LoginState(provider_id=provider_id, mode="browser", state="idle", message="", urls=[], output=[], seconds=0)
    return LoginState(**s)


@router.post("/providers/{provider_id}/login/code", response_model=LoginState, summary="받은 로그인 코드 넘기기 (다른 PC 에서 로그인할 때)")
def login_code(provider_id: str, body: LoginCode) -> LoginState:
    try:
        return LoginState(**claude_login.send(provider_id, body.code))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/providers/{provider_id}/login", response_model=Ok, summary="로그인 그만두기")
def cancel_login(provider_id: str) -> Ok:
    claude_login.cancel(provider_id)
    return Ok()


@router.delete("/providers/{provider_id}", response_model=Ok, summary="연결 지우기 (저장한 키도 지운다)")
def delete_provider(provider_id: str) -> Ok:
    try:
        providers.remove(provider_id)
    except KeyError as e:
        raise HTTPException(404, str(e.args[0]))
    return Ok()


# ───────────────────────── 기록 · 사용량

class AdminRun(RunSummary):
    user_name: str = Field(default="", description="실행한 사람 (아이디 · 이름). 빈 값 = 혼자 쓰기")
    company_name: str = ""


class RunPage(ApiModel):
    total: int
    items: list[AdminRun]


class UsageRow(ApiModel):
    key: str
    label: str
    runs: int
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    last_at: str


def _names() -> tuple[dict[str, str], dict[str, str]]:
    users = {u.id: f"{u.username}" + (f" ({u.display_name})" if u.display_name and u.display_name != u.username else "")
             for u in deps.accounts.users()}
    companies = {c.id: c.name for c in deps.accounts.companies()}
    return users, companies


def _filters(user_id: str | None, company_id: str | None, provider: str | None, connection: str | None,
             model: str | None, since: str | None, until: str | None, status: str | None = None) -> dict[str, Any]:
    return dict(user_id=user_id, company_id=company_id, provider=provider, connection=connection, model=model,
                since=since or None, until=until or None, status=status or None)


@router.get("/runs", response_model=RunPage, summary="전체 실행 기록 (누가 · 어느 회사 · 어떤 모델로)")
def all_runs(user_id: str | None = None, company_id: str | None = None, provider: str | None = None,
             connection: str | None = None, model: str | None = None, status: str | None = None,
             since: str | None = Query(None, description="이 시각부터 (YYYY-MM-DD 또는 ISO)"),
             until: str | None = Query(None, description="이 시각 전까지"),
             limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)) -> RunPage:
    total, rows = deps.store.search(limit, offset, **_filters(user_id, company_id, provider, connection, model, since, until,
                                                              status))
    users, companies = _names()
    return RunPage(total=total, items=[AdminRun(**r, user_name=users.get(r["user_id"], r["user_id"]),
                                                company_name=companies.get(r["company_id"], r["company_id"])) for r in rows])


@router.get("/usage", response_model=list[UsageRow], summary="사용량 합계 (사람 · 회사 · 외부 연결 · 모델별)")
def usage(group: Literal["user_id", "company_id", "connection", "model", "provider"] = "user_id",
          user_id: str | None = None, company_id: str | None = None, provider: str | None = None,
          connection: str | None = None, model: str | None = None,
          since: str | None = None, until: str | None = None) -> list[UsageRow]:
    rows = deps.store.usage(group, **_filters(user_id, company_id, provider, connection, model, since, until))
    users, companies = _names()
    titles = {p.id: p.title for p in providers.all_providers()}
    label = {"user_id": lambda k: users.get(k, k) if k else "(혼자 쓰기)",
             "company_id": lambda k: companies.get(k, k) if k else "(회사 없음)",
             "connection": lambda k: titles.get(k, k) if k else "(로컬 모델)",
             "model": lambda k: k, "provider": lambda k: k or "local"}[group]
    return [UsageRow(key=r["key"] or "", label=label(r["key"] or ""), runs=r["runs"], prompt_tokens=r["prompt_tokens"] or 0,
                     completion_tokens=r["completion_tokens"] or 0, seconds=round(r["seconds"] or 0, 1),
                     last_at=r["last_at"] or "") for r in rows]


# ───────────────────────── 폴더 고르기 (입력창의 '작업 위치' — 로컬 에이전트·CLI 공통)

class FolderEntry(ApiModel):
    name: str
    path: str


class FolderList(ApiModel):
    root: str = Field(description="기준 폴더 (환경변수 AGENT_WORK_ROOT, 없으면 서버 아래 작업 폴더)")
    root_from_env: bool = Field(description="기준 폴더를 환경변수로 정했는지")
    path: str = Field(description="지금 보는 폴더 — 기준 기준 상대 경로 ('' = 기준 폴더)")
    parent: str | None = Field(default=None, description="위 폴더 (상대 경로, null = 기준 폴더라 더 위 없음)")
    dirs: list[FolderEntry] = Field(description="하위 폴더 (path 는 상대 경로)")


@router.get("/fs/dirs", response_model=FolderList, summary="작업 위치 고르기: 기준 폴더 아래 하위 폴더 (상대 경로)")
def browse_dirs(request: Request, path: str | None = None) -> dict[str, Any]:
    try:
        return locations.browse_work_dirs(path, deps.settings_for(deps.user(request)))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
