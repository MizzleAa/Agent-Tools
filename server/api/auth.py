"""로그인 · 비밀번호 (로그인 사용 모드) + 내 계정(사내 API 본인 계정).

    /api/v1/auth/…  로그인 없이 부를 수 있다 — 내 상태, 로그인, 로그아웃, 비밀번호 바꾸기, 비밀번호 찾기(관리자에게 요청),
                    로그인 사용 켜기(첫 관리자 만들기)
    /api/v1/me/…    로그인해야 한다 — 내 사내 API 계정 (각자 자기 계정으로 조회, D1~D3)

로그인 세션은 HttpOnly 쿠키(agent_session)다. 비밀번호·세션 값은 응답·기록에 나가지 않는다.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from agent import accounts, apiauth
from agent.accounts import User
from agent.tools import ToolError
from agent.tools.web import check_url
from server import deps
from server.api.common import ApiModel, ERRORS, Ok

router = APIRouter(tags=["auth"], responses=ERRORS)
SESSION_COOKIE = "agent_session"


# ───────────────────────── 모양

class UserInfo(ApiModel):
    id: str
    username: str
    display_name: str
    company_id: str
    company_name: str = ""
    role: str = Field(description="admin · user")
    active: bool = True
    must_change: bool = Field(description="임시 비밀번호 — 바꾸기 전에는 다른 기능을 못 쓴다")
    password_expired: bool = Field(description="마지막으로 바꾼 지 설정한 날수(기본 30일)가 지남 — 바꾸라고 안내 ('나중에' 가능)")
    password_changed_at: str
    created_at: str
    last_login_at: str


class AuthState(ApiModel):
    app_name: str = Field(description="화면에 보이는 서비스 이름 (관리 › 접속 설정)")
    login_enabled: bool
    user: UserInfo | None = Field(default=None, description="로그인한 사람. 혼자 쓰기(로그인 없음)면 null")
    password_max_age_days: int
    offline: bool
    has_admin: bool = Field(description="관리자 계정이 있는지 (없으면 '첫 관리자 만들기')")
    setup_allowed: bool = Field(description="지금 이 요청으로 첫 관리자를 만들 수 있는지 (관리자가 없고, 서버 PC 에서 들어왔거나 혼자 쓰기)")


class LoginBody(ApiModel):
    username: str
    password: str


class PasswordChange(ApiModel):
    current_password: str
    new_password: str


class ResetRequestBody(ApiModel):
    username: str = Field(description="아이디")
    company: str = Field(default="", description="소속 회사 (관리자가 알아보게)")
    note: str = Field(default="", description="연락처 등 남길 말")


class SetupBody(ApiModel):
    username: str
    password: str
    display_name: str = ""


class MyApiAccount(ApiModel):
    name: str
    title: str
    base_url: str
    has_account: bool


class MyApiCredentials(ApiModel):
    username: str
    password: str


class CheckResult(ApiModel):
    ok: bool
    message: str


# ───────────────────────── 도움

def user_info(user: User, max_age_days: int | None = None) -> UserInfo:
    days = accounts.load_access().password_max_age_days if max_age_days is None else max_age_days
    company = deps.accounts.company(user.company_id) if user.company_id else None
    return UserInfo(**{k: v for k, v in user.public().items() if k in UserInfo.model_fields},
                    company_name=company.name if company else "",
                    password_expired=deps.accounts.password_expired(user, days))


LOCAL_CLIENTS = ("127.0.0.1", "::1", "localhost", "testclient")


def from_server_pc(request: Request | None) -> bool:
    return request is not None and (request.client.host if request.client else "") in LOCAL_CLIENTS


def state(user: User | None, request: Request | None = None) -> AuthState:
    access = accounts.load_access()
    has_admin = deps.accounts.admin_count() > 0
    return AuthState(app_name=access.app_name, login_enabled=access.login_enabled,
                     user=user_info(user, access.password_max_age_days) if user else None,
                     password_max_age_days=access.password_max_age_days, offline=access.offline, has_admin=has_admin,
                     setup_allowed=not has_admin and (not access.login_enabled or from_server_pc(request)))


def _set_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", path="/",
                        secure=request.url.scheme == "https")


# ───────────────────────── 로그인

@router.get("/auth/me", response_model=AuthState, summary="접속 방식과 로그인한 사람 (로그인 전에도 부를 수 있다)")
def me(request: Request) -> AuthState:
    return state(deps.user(request), request)


@router.post("/auth/login", response_model=AuthState, summary="로그인 (세션 쿠키를 준다)")
def login(body: LoginBody, request: Request, response: Response) -> AuthState:
    access = accounts.load_access()
    if not access.login_enabled:
        raise HTTPException(409, "로그인 없이 쓰는 '혼자 쓰기' 모드입니다")
    try:
        user, token = deps.accounts.login(body.username, body.password, access.session_hours)
    except ValueError as e:
        raise HTTPException(401, str(e))
    _set_cookie(response, request, token)
    return state(user, request)


@router.post("/auth/logout", response_model=Ok, summary="로그아웃 (이 세션을 끝낸다)")
def logout(request: Request, response: Response) -> Ok:
    if token := request.cookies.get(SESSION_COOKIE):
        deps.accounts.logout(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return Ok()


@router.post("/auth/password", response_model=AuthState, summary="내 비밀번호 바꾸기 (다른 곳의 로그인은 끝난다)")
def change_password(body: PasswordChange, request: Request) -> AuthState:
    user = deps.user(request)
    if user is None:
        raise HTTPException(401, "로그인이 필요합니다")
    try:
        user = deps.accounts.change_password(user.id, body.current_password, body.new_password,
                                             keep_token=request.cookies.get(SESSION_COOKIE, ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return state(user, request)


@router.post("/auth/reset-requests", response_model=Ok, summary="비밀번호 찾기: 관리자에게 재설정 요청 (아이디가 없어도 같은 답)")
def request_reset(body: ResetRequestBody) -> Ok:
    if not accounts.load_access().login_enabled:
        raise HTTPException(409, "로그인 없이 쓰는 '혼자 쓰기' 모드입니다")
    try:
        deps.accounts.request_reset(body.username, body.company, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Ok()


@router.post("/auth/setup", response_model=AuthState, summary="첫 관리자 만들기 (· 혼자 쓰기에서 로그인 사용 다시 켜기)")
def setup(body: SetupBody, request: Request, response: Response) -> AuthState:
    """관리자 계정이 없으면 첫 관리자를 만들고 로그인 사용을 켠다 — 로그인 사용 중이면 서버 PC 에서만 (다른 PC 에서 먼저 들어온
    사람이 관리자를 가로채지 못하게). 첫 관리자를 만들면 지금까지의 기록은 그 관리자 것이 된다.
    관리자가 이미 있으면 혼자 쓰기에서만 부를 수 있다 — 그 관리자 계정이 맞는지 확인하고 로그인 사용을 다시 켠다."""
    access = accounts.load_access()
    first = deps.accounts.admin_count() == 0
    if first and access.login_enabled and not from_server_pc(request):
        raise HTTPException(403, "첫 관리자 계정은 서버 PC 에서 만듭니다 (서버 PC 의 브라우저로 http://127.0.0.1:8765 에 들어가세요)")
    if not first and access.login_enabled:
        raise HTTPException(409, "이미 로그인 사용 중입니다")
    if first:
        try:
            admin, _ = deps.accounts.create_user(body.username, display_name=body.display_name or body.username,
                                                 role=accounts.ROLE_ADMIN, password=body.password)
        except ValueError as e:
            raise HTTPException(400, str(e))
        deps.store.assign_orphans(admin.id)
    else:
        found = deps.accounts.by_username(body.username)
        if found is None or not found.is_admin:
            raise HTTPException(400, "관리자 계정의 아이디·비밀번호를 넣으세요 (이미 관리자 계정이 있습니다)")
    try:
        user, token = deps.accounts.login(body.username, body.password, access.session_hours)
    except ValueError as e:
        raise HTTPException(400, str(e))
    access.login_enabled = True
    accounts.save_access(access)
    _set_cookie(response, request, token)
    return state(user, request)


# ───────────────────────── 내 사내 API 계정 (로그인 사용 모드)

def _personal_apis() -> list:
    return [a for a in deps.load_settings().apis if a.auth is not None and a.auth.kind == "login"]


def _mine(request: Request) -> User:
    user = deps.user(request)
    if user is None:
        raise HTTPException(409, "혼자 쓰기(로그인 없음)에서는 '상태 · 연결' › 사내 API 의 인증 칸에 넣습니다")
    return user


def _api(name: str):
    api = next((a for a in _personal_apis() if a.name == name), None)
    if api is None:
        raise HTTPException(404, f"'{name}' 은 각자 로그인하는 사내 API 가 아닙니다")
    return api


@router.get("/me/api-accounts", response_model=list[MyApiAccount], summary="각자 로그인하는 사내 API 와 내 계정 저장 여부")
def my_api_accounts(request: Request) -> list[MyApiAccount]:
    _mine(request)
    return [MyApiAccount(name=a.name, title=a.title or a.name, base_url=a.base_url, has_account=not apiauth.missing(a))
            for a in _personal_apis()]


@router.put("/me/api-accounts/{name}", response_model=Ok, summary="내 사내 API 계정 저장 (돌려주지 않는다)")
def set_my_api_account(name: str, body: MyApiCredentials, request: Request) -> Ok:
    user, api = _mine(request), _api(name)
    if not body.username.strip() or not body.password:
        raise HTTPException(400, "아이디와 비밀번호를 넣으세요")
    accounts.set_user_secret(user.id, api.auth.username_secret, body.username.strip())
    accounts.set_user_secret(user.id, api.auth.password_secret, body.password)
    apiauth.forget(api)
    return Ok()


@router.delete("/me/api-accounts/{name}", response_model=Ok, summary="내 사내 API 계정 지우기")
def clear_my_api_account(name: str, request: Request) -> Ok:
    user, api = _mine(request), _api(name)
    accounts.set_user_secret(user.id, api.auth.username_secret, None)
    accounts.set_user_secret(user.id, api.auth.password_secret, None)
    apiauth.forget(api)
    return Ok()


@router.post("/me/api-accounts/{name}/check", response_model=CheckResult, summary="내 계정으로 로그인해 보기 (토큰은 돌려주지 않음)")
def check_my_api_account(name: str, request: Request) -> CheckResult:
    _mine(request)
    api, s = _api(name), deps.load_settings()
    try:
        return CheckResult(ok=True, message=apiauth.check_login(api, lambda u: check_url(s, u)))
    except ToolError as e:
        return CheckResult(ok=False, message=str(e))

