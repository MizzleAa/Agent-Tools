"""관리 서버 (FastAPI): API(/api/v1) + 화면(web/dist, React 빌드 결과).

    .venv\\Scripts\\python -m server            # http://127.0.0.1:8765
    .venv\\Scripts\\python -m server --open     # 브라우저도 연다

구조
- API 는 server/api/ 에 자원별 라우터로 나뉜다 (목록: server/api/__init__.py). 공유 상태는 server/deps.py.
- 화면은 web/ (React + TypeScript + Vite). `npm run build` 결과(web/dist)를 이 서버가 그대로 내보낸다.
  폐쇄망 PC 에는 빌드 결과만 옮기므로 Node·npm 이 없어도 된다.

보안
- Host 헤더를 검사한다 (DNS 리바인딩 방지). 이 PC(127.0.0.1·localhost) + 관리자가 정한 접속 주소(config/access.yaml public_hosts).
- 상태를 바꾸는 요청(GET 이 아닌 것)은 시작할 때 만든 토큰(X-Agent-Token)이 있어야 한다. 이게 없으면 내가 방문한
  아무 웹페이지가 승인 API 를 불러 내 PC 에서 명령을 실행시킬 수 있다. 토큰은 index.html 에 넣어 화면에 준다.
- 로그인 사용(access.yaml login_enabled)이면 /api/v1/auth/* 밖의 API 는 로그인 세션(쿠키)이 있어야 한다.
  화면(index.html·assets)은 로그인 화면을 그려야 하므로 열려 있다. 임시 비밀번호로 들어온 사람은 바꾸기 전까지 auth API 만 쓴다.
"""

from __future__ import annotations

import html
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent import accounts, crawler
from agent.claude_code.errors import AppError
from agent.config import ROOT
from agent.runtime import health
from server import deps
from server.api import VERSION
from server.api import router as api_router
from server.api import claude_code as claude_code_api
from server.api.common import validation_text

WEB_DIST = ROOT / "web" / "dist"
TOKEN_PLACEHOLDER = "__AGENT_TOKEN__"
NAME_PLACEHOLDER = "__APP_NAME__"   # index.html 의 <title> — 관리 › 접속 설정의 서비스 이름
SESSION_COOKIE = "agent_session"
OPEN_API = (f"/api/{VERSION}/auth/",)   # 로그인 없이 부를 수 있는 API (로그인·비밀번호 찾기·내 상태)


def _prune_images() -> None:
    """보관 날수가 지난 이미지 원본을 지운다 (설정 '이미지 보관' — 썸네일은 남는다). 실패해도 서버는 켠다."""
    from agent import image_store
    try:
        s = deps.load_settings()
        image_store.prune(s.data_path, s.images.keep_days)
    except Exception:   # noqa: BLE001
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 관리 화면을 켜면 모델 서버도 같이 켜고, 끄면 같이 끈다 (우리가 띄운 경우에만).
    # 압축 해제·모델 적재가 오래 걸려도 화면은 바로 뜨도록 백그라운드에서 시작한다.
    _prune_images()
    if deps.settings.runtime.autostart:
        if health(deps.settings) in ("down", "occupied"):   # occupied = 다른 프로그램이 포트를 씀 → 빈 포트로 바꾼다
            deps.llm_server.start_async()
        else:   # 이전 관리 서버 창을 닫아 남은 llama-server 가 있으면 이어받는다 (모델이 다르면 다시 띄움)
            deps.llm_server.adopt_async()
    yield
    await claude_code_api.shutdown()   # Claude Code 탭이 띄운 CLI 프로세스를 정상 종료 (세션 기록은 남는다)
    crawler.cancel_jobs()
    deps.llm_server.stop()
    from agent import embedding
    embedding.stop_server()   # 문서 Q&A 가 띄운 임베딩 서버 (띄운 적 없으면 아무것도 안 한다)


app = FastAPI(title="AI Agent API", version="1.0.0", lifespan=lifespan,
              description="AI Agent 관리 서버 (로컬 모델 · 외부 AI 모델). 화면(web/)은 이 API 만 쓴다. 오류는 항상 {\"detail\": \"설명\"}.",
              openapi_url=f"/api/{VERSION}/openapi.json", docs_url="/api/docs", redoc_url=None)
app.include_router(api_router)


# ───────────────────────── 보안·오류 모양

@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in deps.allowed_hosts():
        return JSONResponse({"detail": "허용되지 않은 Host — 관리자가 '관리 › 접속 설정'에서 이 주소를 접속 주소로 넣어야 합니다"},
                            status_code=403)
    access = accounts.load_access()
    user = None
    if access.login_enabled:
        user = deps.accounts.session_user(request.cookies.get(SESSION_COOKIE, ""), access.session_hours)
        path = request.url.path
        if path.startswith("/api/") and not path.startswith(OPEN_API):
            if user is None:
                return JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
            if user.must_change:
                return JSONResponse({"detail": "임시 비밀번호입니다. 새 비밀번호로 바꾼 뒤에 쓸 수 있습니다."}, status_code=403)
    request.state.user = user
    accounts.CURRENT.set(user)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-agent-token") != deps.TOKEN:
        return JSONResponse({"detail": "토큰이 없거나 틀립니다. 페이지를 새로고침하세요."}, status_code=403)
    return await call_next(request)


@app.exception_handler(AppError)
async def claude_code_error(_: Request, exc: AppError) -> JSONResponse:
    """Claude Code 탭의 오류도 {"detail": 설명}. 화면이 경우를 나눌 수 있게 코드·추가 정보는 머리글로."""
    return JSONResponse({"detail": exc.message}, status_code=exc.status_code,
                        headers={"X-Error-Code": exc.code, "X-Error-Extra": json.dumps(exc.extra, ensure_ascii=True)})


@app.exception_handler(RequestValidationError)
async def invalid_input(_: Request, exc: RequestValidationError) -> JSONResponse:
    """입력 검증 오류도 다른 오류와 같은 모양(400 + detail 문자열)으로."""
    return JSONResponse({"detail": validation_text(exc.errors())}, status_code=400)


def openapi_schema() -> dict:
    """FastAPI 가 붙이는 422 응답을 빼고 (위 처리기로 400 이 된다) 계약을 만든다."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    for path in schema.get("paths", {}).values():
        for op in path.values():
            op.get("responses", {}).pop("422", None)
    for name in ("HTTPValidationError", "ValidationError"):
        schema.get("components", {}).get("schemas", {}).pop(name, None)
    app.openapi_schema = schema
    return schema


app.openapi = openapi_schema   # type: ignore[method-assign]


# ───────────────────────── 화면 (React 빌드 결과)

def _index() -> HTMLResponse:
    index = WEB_DIST / "index.html"
    if not index.exists():
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>로컬 에이전트</title>"
            "<body style='font-family:system-ui;padding:40px;line-height:1.7'>"
            "<h2>화면 빌드가 없습니다</h2><p>인터넷이 되는 PC 에서 아래를 한 번 실행하세요 (폐쇄망 PC 에는 결과 폴더만 옮깁니다).</p>"
            "<pre style='background:#f3f1ea;padding:12px;border-radius:8px'>cd web\nnpm ci\nnpm run build</pre>"
            "<p>API 는 동작합니다: <a href='/api/docs'>/api/docs</a></p></body>", status_code=503)
    page = index.read_text(encoding="utf-8").replace(NAME_PLACEHOLDER, html.escape(accounts.load_access().app_name))
    return HTMLResponse(page.replace(TOKEN_PLACEHOLDER, deps.TOKEN),
                        headers={"Cache-Control": "no-store"})   # 토큰이 들어 있으므로 캐시하지 않는다


if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    return _index()


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    """화면 주소(/settings/... 등)는 React 가 처리하므로 index.html 을 준다. 없는 API 주소는 404 JSON."""
    if path.startswith("api/"):
        return JSONResponse({"detail": "없는 API 주소입니다"}, status_code=404)
    file = (WEB_DIST / path).resolve()
    if file.is_file() and file.is_relative_to(WEB_DIST.resolve()):
        return FileResponse(file)
    return _index()
