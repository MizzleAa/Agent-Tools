"""사내 연결: 수집할 사이트, 사내 API, 읽기 허용 내부 주소, 토큰. 저장 위치는 config/connections.yaml (토큰은 secrets.yaml)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field

from agent import apiauth, connections, crawler, openapi
from agent.tools import ToolError
from agent.tools.web import check_url
from agent.index import DocIndex
from server import deps
from server.api.common import ApiModel, ERRORS, Ok

router = APIRouter(tags=["connections"], responses=ERRORS)


class SourceCreate(ApiModel):
    url: str
    name: str = ""
    description: str = ""
    whole_site: bool = Field(default=False, description="참이면 사이트 전체, 거짓이면 준 주소가 있는 폴더 아래만 수집")


class SourceCreated(ApiModel):
    name: str
    prefix: str


class CrawlRequest(ApiModel):
    source: str = Field(default="", description="비우면 등록된 사이트 전부")


class CrawlStarted(ApiModel):
    started: list[str]


class ApiCreate(ApiModel):
    url: str = Field(description="Swagger/OpenAPI 문서 주소 또는 API 기본 주소")
    name: str = ""


class ApiCreated(ApiModel):
    name: str
    base_url: str
    endpoints: int


class EndpointCount(ApiModel):
    endpoints: int


class WriteAccess(ApiModel):
    allow: bool = Field(description="참이면 POST·PUT·PATCH·DELETE 도 (실행할 때마다 승인을 받고) 쓸 수 있다")


class Methods(ApiModel):
    allow_methods: list[str]


class HostCreate(ApiModel):
    rule: str = Field(description="호스트 이름 또는 CIDR (예: wiki.intra, 10.20.0.0/16)")


class HostRule(ApiModel):
    rule: str


class SecretValue(ApiModel):
    value: str


class ApiAuthUpdate(ApiModel):
    kind: Literal["none", "token", "login"] = Field(description="none = 인증 없음, token = 고정 토큰, login = 계정으로 로그인(OAuth2 password)")
    login_url: str = Field(default="", description="login 일 때 토큰을 받는 주소")
    header: str = "Authorization"
    prefix: str = Field(default="Bearer ", description="token 일 때 헤더 값 앞에 붙일 글자")


class ApiCredentials(ApiModel):
    """값은 저장만 하고 어디로도 돌려주지 않는다 (응답·로그·모델 모두)."""
    token: str = ""
    username: str = ""
    password: str = ""


class LoginCheck(ApiModel):
    ok: bool
    message: str


# ───────────────────────── 사내 사이트 (수집 → search_intranet)

@router.post("/intranet/sources", response_model=SourceCreated, summary="사이트 등록 (바로 수집 시작)")
def add_source(body: SourceCreate) -> SourceCreated:
    s = deps.load_settings()
    try:
        src = connections.add_source(s, body.url, body.name, body.description, body.whole_site)
    except ValueError as e:
        raise HTTPException(400, str(e))
    crawler.start_background(s, src)
    return SourceCreated(name=src.name, prefix=src.include_prefixes[0])


@router.delete("/intranet/sources/{name}", response_model=Ok, summary="사이트 삭제 (수집한 페이지도 색인에서 뺌)")
def delete_source(name: str) -> Ok:
    try:
        connections.remove_source(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    crawler.cancel_jobs(name)
    index_path = deps.load_settings().data_path / "index.db"
    if index_path.exists():
        idx = DocIndex(index_path)
        idx.remove_missing(name, set())
        idx.close()
    return Ok()


@router.post("/intranet/crawls", response_model=CrawlStarted, summary="지금 수집 (다시 수집)")
def start_crawl(body: CrawlRequest) -> CrawlStarted:
    s = deps.load_settings()
    sources = [x for x in s.intranet.sources if not body.source or x.name == body.source]
    if not sources:
        raise HTTPException(404, "등록된 사내 사이트가 없습니다")
    return CrawlStarted(started=[crawler.start_background(s, src).source for src in sources])


@router.post("/intranet/crawls/cancel", response_model=Ok, summary="수집 중지")
def cancel_crawl(body: CrawlRequest) -> Ok:
    crawler.cancel_jobs(body.source)
    return Ok()


# ───────────────────────── 사내 API (api_get / api_send)

@router.post("/apis", response_model=ApiCreated, summary="API 등록 (Swagger 주소면 엔드포인트까지 자동)")
def add_api(body: ApiCreate) -> ApiCreated:
    s = deps.load_settings()
    try:
        api, spec = connections.add_api_from_docs(s, body.url, body.name)
        return ApiCreated(name=api.name, base_url=api.base_url, endpoints=len(openapi.endpoints(spec)))
    except ValueError as e:
        looks_docs = any(k in body.url.lower() for k in ("/docs", "swagger", "redoc", "openapi", "api-docs"))
        if looks_docs or "이미" in str(e) or "로컬" in str(e):
            raise HTTPException(400, str(e))
    try:   # 문서가 없는 일반 API 주소: 엔드포인트 없이 등록 (나중에 채팅으로 알려주면 된다)
        api = connections.add_api(s, body.name, body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ApiCreated(name=api.name, base_url=api.base_url, endpoints=0)


@router.put("/apis/{name}/auth", response_model=Ok, summary="API 인증 방식 정하기 (값은 credentials 로 따로)")
def set_api_auth(name: str, body: ApiAuthUpdate) -> Ok:
    try:
        connections.set_api_auth(deps.load_settings(), name, body.kind, body.login_url, body.header, body.prefix)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Ok()


@router.put("/apis/{name}/credentials", response_model=Ok, summary="API 토큰 또는 계정 저장 (config/secrets.yaml, 돌려주지 않음)")
def set_api_credentials(name: str, body: ApiCredentials) -> Ok:
    try:
        connections.set_api_credentials(deps.load_settings(), name, body.token, body.username, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Ok()


@router.post("/apis/{name}/login-check", response_model=LoginCheck, summary="저장한 계정으로 로그인해 보기 (토큰은 돌려주지 않음)")
def login_check(name: str) -> LoginCheck:
    s = deps.load_settings()
    api = next((a for a in s.apis if a.name == name), None)
    if api is None or api.auth is None:
        raise HTTPException(404, f"'{name}' 은 인증 방식이 정해진 API 가 아닙니다")
    try:
        if api.auth.kind == "login":
            return LoginCheck(ok=True, message=apiauth.check_login(api, lambda u: check_url(s, u)))
        apiauth.headers(api)
        return LoginCheck(ok=True, message="토큰이 저장돼 있습니다 (맞는지는 조회해 봐야 압니다)")
    except ToolError as e:
        return LoginCheck(ok=False, message=str(e))


@router.delete("/apis/{name}", response_model=Ok, summary="API 삭제")
def delete_api(name: str) -> Ok:
    try:
        connections.remove_api(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Ok()


@router.post("/apis/{name}/refresh", response_model=EndpointCount, summary="Swagger 문서를 다시 읽어 엔드포인트 갱신")
def refresh_api(name: str) -> EndpointCount:
    try:
        return EndpointCount(endpoints=connections.refresh_api_docs(deps.load_settings(), name))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/apis/{name}/write-access", response_model=Methods, summary="변경 요청(POST 등) 허용 켜기·끄기")
def set_write_access(name: str, body: WriteAccess) -> Methods:
    try:
        return Methods(allow_methods=connections.set_api_write(name, body.allow))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ───────────────────────── 읽기 허용 내부 주소 (fetch_url)

@router.post("/hosts", response_model=HostRule, summary="내부 주소 읽기 허용")
def add_host(body: HostCreate) -> HostRule:
    try:
        return HostRule(rule=connections.allow_host(deps.load_settings(), body.rule))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/hosts", response_model=Ok, summary="내부 주소 허용 지우기 (CIDR 에 / 가 있어 쿼리로 받는다)")
def delete_host(rule: str = Query(...)) -> Ok:
    try:
        connections.remove_host(rule)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Ok()


# ───────────────────────── 토큰 (config/secrets.yaml — 화면·모델 어디에도 다시 보여주지 않는다)

@router.put("/secrets/{name}", response_model=Ok, summary="토큰 저장")
def save_secret(name: str, body: SecretValue) -> Ok:
    if not body.value.strip():
        raise HTTPException(400, "값을 입력하세요")
    try:
        connections.set_secret(name, body.value.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Ok()
