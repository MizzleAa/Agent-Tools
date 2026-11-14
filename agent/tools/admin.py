"""채팅으로 사내 사이트·API 를 등록하는 도구.

"http://wiki.intra/ 추가해줘" → add_intranet_site → connections.yaml 에 저장 + 바로 수집 시작.
에이전트가 접근할 수 있는 범위를 넓히는 일이라 permission="admin" (항상 사람의 승인).
토큰 값은 받지 않는다. 채팅에 쓰면 모델이 보게 되므로, 관리 화면에서 입력하게 안내한다.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from agent import connections
from agent import crawler  # 모듈째로 가져온다 (crawler 가 tools 를 import 하므로 순환 import 방지)
from agent.tools.base import ToolContext, ToolError, tool
from agent.tools.web import _download, auth_headers


@tool("admin")
def add_intranet_site(
    ctx: ToolContext,
    url: Annotated[str, Field(description="등록할 사내 페이지 주소 (http/https)")],
    name: Annotated[str, Field(description="짧은 영문 이름. 비우면 주소에서 만든다")] = "",
    description: Annotated[str, Field(description="어떤 사이트인지 한 줄 설명")] = "",
    whole_site: Annotated[bool, Field(description="true 면 사이트 전체, false 면 준 주소가 있는 폴더 아래만 수집")] = False,
) -> str:
    """사내 웹사이트를 검색 대상으로 등록·저장하고 바로 수집을 시작한다. 사용자가 주소를 추가해 달라고 할 때 쓴다."""
    try:
        src = connections.add_source(ctx.settings, url, name, description, whole_site)
    except ValueError as e:
        raise ToolError(str(e))
    lines = [f"등록 완료: '{src.name}' (config/connections.yaml 에 저장)",
             f"  시작 주소: {src.start_urls[0]}",
             f"  수집 범위: {src.include_prefixes[0]} 로 시작하는 페이지 (최대 {src.max_pages}쪽, 링크 {src.max_depth}단계)",
             f"  건너뛰는 링크: {', '.join(src.exclude_patterns)}"]
    try:
        _download(ctx.settings, src.start_urls[0], auth_headers(src.auth), src.verify_tls)
    except ToolError as e:
        lines.append(f"주의: 지금은 이 주소에 접속되지 않습니다 ({e}). 등록은 해 두었으니 접속되는 환경에서 "
                     "관리 화면 '상태' 탭의 '지금 수집'을 누르세요.")
        return "\n".join(lines)
    crawler.start_background(ctx.settings, src)
    lines.append("접속 확인 OK. 수집을 백그라운드로 시작했습니다. 진행 상황은 관리 화면 '상태' 탭에서 보이며, "
                 "수집되는 대로 search_intranet 으로 검색할 수 있습니다.")
    return "\n".join(lines)


@tool("read")
def crawl_site(
    ctx: ToolContext,
    name: Annotated[str, Field(description="다시 수집할 사이트 이름. 비우면 전부")] = "",
) -> str:
    """등록된 사내 사이트를 다시 수집해 색인을 최신으로 만든다 (바뀐 페이지만 갱신). 이미 수집 중이면 진행 상황을 알려준다."""
    sources = [s for s in ctx.settings.intranet.sources if not name or s.name == name]
    if not sources:
        names = ", ".join(s.name for s in ctx.settings.intranet.sources) or "(없음)"
        raise ToolError(f"등록된 사이트가 아닙니다: {name}. 등록된 사이트: {names}")
    lines = []
    for src in sources:
        before = crawler.job_status(src.name)
        st = crawler.start_background(ctx.settings, src)
        if before is st:
            lines.append(f"- {src.name}: 이미 수집 중 ({st.pages}쪽 진행)")
        else:
            lines.append(f"- {src.name}: 수집 시작")
    return "\n".join(lines) + "\n진행 상황은 관리 화면 '상태' 탭에서 볼 수 있습니다."


_DOCS_HINT = ("/docs", "swagger", "redoc", "openapi", "api-docs")


@tool("admin")
def add_api(
    ctx: ToolContext,
    url: Annotated[str, Field(description="Swagger/OpenAPI 문서 주소(예: http://10.1.2.3:20000/docs) 또는 API 기본 주소")],
    name: Annotated[str, Field(description="짧은 영문 이름. 비우면 문서 제목에서 만든다")] = "",
    description: Annotated[str, Field(description="어떤 시스템인지 한 줄 설명. 비우면 문서에서 가져온다")] = "",
    docs: Annotated[str, Field(description="Swagger 문서가 없을 때만: 사용자가 알려준 엔드포인트 목록 ('GET /lots/{id}  로트 상세')")] = "",
    auth_secret: Annotated[str, Field(description="인증이 필요하면 토큰 이름 (예: MES_TOKEN). 토큰 값 자체는 절대 넣지 않는다")] = "",
    auth_prefix: Annotated[str, Field(description="헤더 값 앞에 붙일 글자. 보통 'Bearer '")] = "Bearer ",
    docs_file: Annotated[str, Field(description="서버 문서가 안 열려 사용자가 준 문서 파일(저장한 Swagger HTML·openapi.json)의 경로. 이때 url 은 API 기본 주소")] = "",
) -> str:
    """사내 REST API 를 등록·저장한다. Swagger 문서 주소를 주면 엔드포인트 목록을 자동으로 읽는다. 사용자에게 엔드포인트를 묻기 전에 이걸 먼저 쓴다."""
    spec = None
    source = ""
    try:
        if docs_file.strip():   # QA API-14·15: 사용자가 준 파일로 등록하고, 서버 문서는 읽지 않는다
            from agent import openapi
            from agent.safety import SandboxError
            from agent.tools.web import local_path
            try:
                path = ctx.sandbox.resolve(local_path(docs_file))
            except SandboxError as e:
                raise ToolError(f"문서 파일을 읽을 수 없습니다: {e}")
            spec = openapi.load_local(path) if path.is_file() else None
            if spec is None:
                raise ToolError(f"이 파일에서 API 목록을 읽지 못했습니다: {path} (OpenAPI JSON 이나 브라우저로 저장한 Swagger 화면이어야 함)")
            info = spec.get("info") or {}
            from agent.connections import secret_base
            auth = openapi.auth_from_spec(spec, url, secret_base(name or info.get("title", ""), url))
            api = connections.add_api(ctx.settings, name or info.get("title", ""), url,
                                      description or info.get("title", ""), openapi.docs_text(spec), auth=auth)
            source = f"엔드포인트 출처: 사용자가 준 문서 파일 {path} (서버 문서는 읽지 않음)"
        elif docs.strip():
            api = connections.add_api(ctx.settings, name, url, description, docs, auth_secret,
                                      auth_prefix=auth_prefix if auth_secret else "")
        else:
            try:
                api, spec = connections.add_api_from_docs(ctx.settings, url, name, description, auth_secret, auth_prefix)
                source = f"엔드포인트 출처: 서버의 API 명세 ({url} 에서 찾아 읽음)"
            except ValueError as e:
                if any(k in url.lower() for k in _DOCS_HINT) or "이미" in str(e) or "로컬" in str(e):
                    raise
                api = connections.add_api(ctx.settings, name, url, description, "", auth_secret,
                                          auth_prefix=auth_prefix if auth_secret else "")
    except ValueError as e:
        raise ToolError(str(e))

    from agent import apiauth

    out = [f"등록 완료: API '{api.name}' → {api.base_url} (config/connections.yaml 에 저장, 조회 GET 만 허용)"]
    if source:   # QA API-15: 어디서 읽었는지 밝힌다 (사용자가 준 자료와 다르면 답에도 그렇게 쓴다)
        out.append(source)
    if name.strip() and api.name != name.strip():   # QA API-7: 이름이 바뀌면 말한다
        out.append(f"요청한 이름 '{name.strip()}' 은 도구에서 '{api.name}' 로 부른다 (소문자·숫자·한글·- 만 씀. 원래 이름도 함께 저장)")
    if spec is not None:
        from agent import openapi
        eps = openapi.endpoints(spec)
        changing = [e for e in eps if e["method"] != "GET"]
        out.append(f"Swagger 문서에서 엔드포인트 {len(eps)}개를 읽었습니다 (조회 {len(eps) - len(changing)}, 변경 {len(changing)}).")
        out.append(f"기능 그룹(전체): {openapi.tag_overview(eps)}")
        out += openapi.security_lines(openapi.security_info(spec, url if docs_file.strip() else (api.docs_url or url)))
        out += ["  " + openapi.endpoint_line(e) for e in eps[:20]]
        if len(eps) > 20:
            out.append(f"  … 외 {len(eps) - 20}개 — 필요한 경로는 find_endpoint(api='{api.name}', query='영어 키워드') 로 찾는다")
        if changing:   # 참고만: 조회(GET)에는 상관없다 (A01 에서 GET 조회에 '변경 요청 허용 확인'을 안내했다)
            out.append(f"(참고: 변경 요청 {len(changing)}개는 막혀 있다. 조회만 할 때는 상관없고, 데이터를 바꿔야 할 때만 사용자가 "
                       "관리 화면에서 '변경 요청 허용'을 켠다)")
    elif not api.docs:
        out.append("엔드포인트 목록을 모릅니다. Swagger 문서 주소(/docs)가 있는지, 없으면 어떤 경로가 있는지 사용자에게 물어보세요.")
    if api.auth is not None:
        out.append(f"인증: {apiauth.describe(api)}")
        if apiauth.missing(api):
            what = "아이디·비밀번호" if api.auth.kind == "login" else f"토큰 '{api.auth.secret}'"
            out.append(f"{what} 이(가) 아직 없습니다. 보안상 채팅으로 받지 말고, 사용자가 {apiauth.ui_place(api)} 에 넣도록 "
                       "안내하세요. 넣고 나면 api_get 이 알아서 인증합니다 (로그인 방식이면 로그인까지).")
    return "\n".join(out)


@tool("admin")
def allow_host(
    ctx: ToolContext,
    host: Annotated[str, Field(description="허용할 내부 주소. 호스트·IP·대역(CIDR) 또는 URL. 예) 10.1.2.3")],
) -> str:
    """내부망 주소를 읽기 허용 목록에 추가·저장한다. fetch_url 등이 '내부망 주소라 차단'으로 실패했을 때 쓴다."""
    try:
        rule = connections.allow_host(ctx.settings, host)
    except ValueError as e:
        raise ToolError(str(e))
    return (f"허용 완료: {rule} (config/connections.yaml 에 저장). 이제 이 주소의 페이지를 fetch_url 로 읽을 수 있습니다. "
            "API 로 쓰려면 add_api 로 등록하세요.")
