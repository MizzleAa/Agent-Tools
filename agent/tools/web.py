"""웹 도구: 검색과 페이지 가져오기. 둘 다 taints=True 라서, 이 도구를 쓴 실행은 그 뒤로 쓰기 자동 승인이 꺼진다
(웹 페이지 속 숨은 지시 = 프롬프트 인젝션 대비).

- web_search : 검색 제공자를 설정으로 바꾼다. ddgs(인터넷) / searxng(사내 서버) / none(폐쇄망)
- fetch_url  : 페이지 본문만 뽑아 돌려준다. 내부망·로컬 주소는 기본 차단 (llama-server, 관리 화면 보호)
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from typing import Annotated
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura
from pydantic import Field

from agent.config import Auth, Settings, get_secret
from agent.tools.base import ToolContext, ToolError, decode_bytes, tool

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LocalAgent/0.1"
PAGE_CHARS = 3500            # fetch_url 한 번에 돌려주는 본문 길이 (루프의 출력 자르기보다 작게)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
CACHE_SECONDS = 600
MIN_HTML_TEXT = 100          # HTML 인데 본문이 이보다 짧으면 자바스크립트로 그리는 페이지로 본다
# url → (가져온 시각, 최종 주소, 제목, 본문, HTML 여부). offset 으로 이어 읽을 때 같은 페이지를 다시 받지 않기 위함
_pages: OrderedDict[str, tuple[float, str, str, str, bool]] = OrderedDict()


# ───────────────────────── 검색

REGION_LABEL = {"kr-kr": "한국", "us-en": "미국", "wt-wt": "전체", "jp-jp": "일본", "uk-en": "영국"}
SEARXNG_LANG = {"kr-kr": "ko-KR", "us-en": "en-US", "jp-jp": "ja-JP", "uk-en": "en-GB"}


def _search_ddgs(settings: Settings, query: str, n: int, region: str) -> list[dict[str, str]]:
    try:
        from ddgs import DDGS
    except ImportError:
        raise ToolError("ddgs 패키지가 없습니다. 폐쇄망이면 settings.yaml 의 web.search_provider 를 none 또는 searxng 로 바꾸세요")
    rows = DDGS(timeout=int(settings.web.fetch_timeout)).text(query, region=region, max_results=n)
    return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in rows]


def _search_searxng(settings: Settings, query: str, n: int, region: str) -> list[dict[str, str]]:
    base = settings.web.searxng_url.rstrip("/")
    if not base:
        raise ToolError("web.searxng_url 이 비어 있습니다 (settings.yaml)")
    params = {"q": query, "format": "json", "language": SEARXNG_LANG.get(region, "all")}
    r = httpx.get(f"{base}/search", params=params, timeout=settings.web.fetch_timeout)
    r.raise_for_status()  # 403 이면 SearXNG settings.yml 의 search.formats 에 json 이 빠진 것
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in r.json().get("results", [])[:n]]


def _merge(per_region: list[tuple[str, list[dict[str, str]]]], limit: int) -> list[dict]:
    """지역별 결과를 번갈아 섞고(한국1, 미국1, 한국2, ...) 같은 주소는 한 번만 남긴다 (나온 지역은 모두 표시)."""
    by_url: dict[str, dict] = {}
    for group in zip_longest(*[rows for _, rows in per_region]):
        for (region, _), row in zip(per_region, group):
            if not row or not row["url"]:
                continue
            if row["url"] in by_url:
                if region not in by_url[row["url"]]["regions"]:
                    by_url[row["url"]]["regions"].append(region)
            else:
                by_url[row["url"]] = {**row, "regions": [region]}
    return list(by_url.values())[:limit]


@tool("read", taints=True)
def web_search(
    ctx: ToolContext,
    query: Annotated[str, Field(description="검색어. 핵심 단어 위주로 짧게")],
    query_en: Annotated[str, Field(description="미국 검색에 쓸 영어 검색어. 한국어 질문이면 영어로 옮겨 넣는다")] = "",
    max_results: Annotated[int, Field(description="결과 개수 (모든 지역 합계)", ge=1, le=10)] = 8,
) -> str:
    """웹을 한국+미국에서 검색해 제목·주소·요약 목록을 돌려준다. 내용 확인은 fetch_url 로 페이지를 읽는다."""
    web_cfg = ctx.settings.web
    if web_cfg.search_provider == "none":
        raise ToolError("웹 검색이 꺼져 있습니다 (폐쇄망 설정). 주소를 알면 fetch_url 로 직접 읽으세요")
    search = _search_ddgs if web_cfg.search_provider == "ddgs" else _search_searxng
    regions = web_cfg.regions or ["wt-wt"]
    limit = min(max_results, web_cfg.max_results)

    def one(region: str):
        # 검색 엔진은 지역보다 검색어의 언어를 따라가므로, 영어권 지역에는 영어 검색어를 쓴다
        q = query_en.strip() if region.endswith("-en") and query_en.strip() else query
        try:   # 지역끼리 겹치는 결과가 많아서, 지역마다 limit 개씩 받은 뒤 섞으면서 줄인다
            return region, search(ctx.settings, q, limit, region), ""
        except ToolError:
            raise
        except Exception as e:
            return region, [], f"{REGION_LABEL.get(region, region)} 검색 실패 ({type(e).__name__})"

    with ThreadPoolExecutor(max_workers=len(regions)) as pool:   # 지역별 검색을 동시에
        results = list(pool.map(one, regions))
    errors = [err for _, _, err in results if err]
    rows = _merge([(region, rows) for region, rows, _ in results], limit)
    if not rows:
        if errors:
            raise ToolError("; ".join(errors) + ". 인터넷 연결을 확인하거나 잠시 뒤 다시 시도하세요")
        return "검색 결과가 없습니다. 검색어를 바꿔 보세요."
    label = lambda r: "·".join(REGION_LABEL.get(x, x) for x in r["regions"])  # noqa: E731
    lines = [f"{i}. [{label(r)}] {r['title']}\n   {r['url']}\n   {r['snippet'][:200]}" for i, r in enumerate(rows, 1)]
    if errors:
        lines.append(f"(참고: {'; '.join(errors)})")
    return "\n".join(lines)


# ───────────────────────── 페이지 가져오기

def _allowed_by_rule(host: str, ips: set, rules: list[str]) -> bool:
    for rule in rules:
        try:
            net = ipaddress.ip_network(rule, strict=False)
            if any(ip in net for ip in ips):
                return True
        except ValueError:
            rule = rule.lower().lstrip(".")
            if host == rule or host.endswith("." + rule):
                return True
    return False


def check_url(settings: Settings, url: str) -> None:
    """http(s) 만, 그리고 내부망·로컬 주소는 allow_private_hosts 에 있을 때만 허용한다 (SSRF 방지)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolError(f"http/https 주소만 가져올 수 있습니다: {url}")
    host = (parts.hostname or "").lower()
    if not host:
        raise ToolError(f"주소가 올바르지 않습니다: {url}")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ToolError(f"주소를 찾을 수 없습니다: {host}")
    ips = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}
    internal = sorted(str(ip) for ip in ips if not ip.is_global)
    rules = settings.web.allow_private_hosts + settings.registered_hosts()   # 등록된 사내 API·사이트는 자동 허용
    if internal and not _allowed_by_rule(host, ips, rules):
        raise ToolError(f"내부망 주소라 차단되었습니다 (아직 허용 안 됨): {host} ({', '.join(internal)}). "
                        f"사용자에게 설정 수정을 부탁하지 말고 직접 등록하세요 — 읽기만 할 거면 allow_host(host='{host}'), "
                        "API(Swagger 문서 포함)면 add_api, 수집·검색할 사이트면 add_intranet_site. 실행 전에 사용자 승인 창이 뜹니다. "
                        "(이 도구가 없는 프로필이면 intranet 프로필로 바꾸라고 안내하세요)")


def auth_headers(auth: Auth | None) -> dict[str, str]:
    if auth is None:
        return {}
    value = get_secret(auth.secret)
    if not value:
        raise ToolError(f"인증 값 '{auth.secret}' 이 설정되지 않았습니다 (환경변수 또는 config/secrets.yaml)")
    return {auth.header: auth.prefix + value}


def _source_for(settings: Settings, url: str):
    """이 주소가 등록된 사내 사이트에 속하면 그 설정(인증·TLS)을 돌려준다."""
    return next((s for s in settings.intranet.sources if any(url.startswith(p) for p in s.prefixes())), None)


def _download(settings: Settings, url: str, headers: dict[str, str] | None = None,
              verify: bool = True) -> tuple[str, str, bytes]:
    """리다이렉트를 직접 따라가며 매 단계 주소를 다시 검사한다 (외부 → 내부 리다이렉트 우회 차단)."""
    limit = settings.web.max_fetch_bytes
    origin = urlsplit(url).netloc
    try:
        with httpx.Client(timeout=settings.web.fetch_timeout, follow_redirects=False, verify=verify,
                          headers={"User-Agent": USER_AGENT, "Accept-Language": "ko,en;q=0.8"}) as client:
            for _ in range(6):
                check_url(settings, url)
                # 인증 헤더는 처음 주소와 같은 호스트에만 보낸다 (다른 호스트로 리다이렉트되면 빼고 보냄)
                same_host = urlsplit(url).netloc == origin
                with client.stream("GET", url, headers=(headers or {}) if same_host else {}) as r:
                    if r.is_redirect and "location" in r.headers:
                        url = urljoin(url, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        raise ToolError(f"HTTP {r.status_code}: {url}")
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) >= limit:
                            break
                    return url, r.headers.get("content-type", ""), bytes(body[:limit])
    except httpx.HTTPError as e:   # 접속 거부·시간 초과·인증서 오류 등 → 모델이 읽을 수 있는 오류로
        raise ToolError(f"접속 실패 ({type(e).__name__}): {url}")
    raise ToolError("리다이렉트가 너무 많습니다")


def _extract(content_type: str, raw: bytes, url: str) -> tuple[str, str, bool]:
    """(제목, 본문, HTML 여부)"""
    ctype = content_type.lower()
    from agent import documents
    suffix = next((s for t, s in documents.SUFFIX_FOR_TYPE.items() if t in ctype), "")
    if not suffix and re.search(r"\.(pdf|docx|xlsx|xlsm)(?:$|\?)", url, re.I):
        suffix = "." + re.search(r"\.(pdf|docx|xlsx|xlsm)(?:$|\?)", url, re.I).group(1).lower()
    if suffix in (".pdf", ".docx", ".xlsx", ".xlsm"):   # 사내 공유 링크가 가리키는 문서 (결정 ③)
        doc = documents.from_bytes(raw, suffix)
        if not doc.text.strip():
            raise ToolError(doc.note or "문서에서 글을 읽지 못했습니다")
        return url.rsplit("/", 1)[-1], doc.text + (f"\n\n({doc.note})" if doc.note else ""), False
    if "html" in ctype or raw.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        html = decode_bytes(raw)
        m = _TITLE.search(html)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        text = trafilatura.extract(html, url=url, include_tables=True, include_comments=False, favor_recall=True)
        if not text:  # 본문 추출 실패 시: 태그만 걷어낸다
            text = re.sub(r"\s+", " ", re.sub(r"(?s)<(script|style).*?</\1>|<[^>]+>", " ", html)).strip()
        return title, text, True
    if ctype.startswith("text/") or "json" in ctype or "xml" in ctype or not ctype:
        return "", decode_bytes(raw), False
    raise ToolError(f"텍스트가 아닌 파일입니다 ({content_type})")


def _as_api_docs(settings: Settings, url: str, raw: bytes, page: tuple[str, str, bool],
                 headers: dict[str, str], verify: bool) -> tuple[str, str, bool] | None:
    """Swagger UI / ReDoc 페이지나 OpenAPI JSON 이면 엔드포인트 요약으로 바꾼다 (JS 로 그려져 본문이 비어 있으므로)."""
    from agent import openapi   # openapi 가 이 모듈을 쓰므로 순환 import 를 피한다

    text = decode_bytes(raw)
    try:
        if page[2] and openapi.looks_like_docs_page(text):
            spec_url, spec = openapi.load_spec(settings, url, headers, verify, html=text)
        elif openapi.is_spec(openapi._json(text)):
            spec_url, spec = url, openapi._json(text)
        else:
            return None
    except ToolError:
        return None
    title, summary = openapi.describe(spec, spec_url)
    return title, summary, False


def _is_local(url: str) -> bool:
    """http(s) 가 아닌 파일 경로 (file:///C:/…, C:\\…, 상대 경로)."""
    low = url.strip().lower()
    return low.startswith("file:") or "://" not in low


def local_path(url: str) -> str:
    """file:///C:/a%20b.html → C:\\a b.html , file://서버/공유/x → \\\\서버\\공유\\x (네트워크 공유)"""
    from agent import locations
    return locations.to_path(url)


def _read_local(ctx: ToolContext, url: str) -> tuple[str, str, str, bool]:
    """작업 폴더(허용 폴더)의 문서 파일 → (주소, 제목, 본문, HTML 여부). API 문서면 엔드포인트 요약으로 (QA API-14)."""
    from agent import openapi
    from agent.safety import SandboxError

    from agent import documents, locations
    try:
        path = ctx.sandbox.resolve(local_path(url))
    except SandboxError as e:
        raise ToolError(f"이 파일은 읽을 수 없습니다: {e}")
    if not path.is_file():
        if path.is_dir():
            raise ToolError(f"폴더입니다 — 폴더는 list_dir 로 봅니다: {path}")
        raise ToolError(locations.explain(FileNotFoundError(str(path)), str(path)))
    if path.suffix.lower() in documents.OFFICE_EXT | {".csv", ".tsv"}:   # 엑셀·PDF·워드·CSV (결정 ③)
        doc = documents.read(path)
        return str(path), path.name, doc.text + (f"\n\n({doc.note})" if doc.note else ""), False
    spec = openapi.load_local(path)
    if spec is not None:
        title, summary = openapi.describe(spec, str(path), local=True)
        return str(path), title, summary, False
    raw = path.read_bytes()[: ctx.settings.web.max_fetch_bytes]
    suffix = path.suffix.lower()
    ctype = "text/html" if suffix in (".html", ".htm") else "application/json" if suffix == ".json" else "text/plain"
    title, text, is_html = _extract(ctype, raw, path.as_uri())
    return str(path), title or path.name, text, is_html


@tool("read", taints=True)
def fetch_url(
    ctx: ToolContext,
    url: Annotated[str, Field(description="가져올 페이지 주소 (http/https) 또는 작업 폴더의 문서 파일 경로")],
    offset: Annotated[int, Field(description="본문의 몇 번째 글자부터 읽을지. 긴 페이지를 이어 읽을 때 사용", ge=0)] = 0,
) -> str:
    """웹 페이지(또는 작업 폴더의 문서 파일)를 가져와 광고·메뉴를 뺀 본문 텍스트를 돌려준다. 긴 페이지는 offset 으로 나눠 읽는다."""
    cached = _pages.get(url)
    if (cached is None or time.monotonic() - cached[0] > CACHE_SECONDS) and _is_local(url):
        _pages[url] = (time.monotonic(), *_read_local(ctx, url))
        cached = _pages[url]
    if cached is None or time.monotonic() - cached[0] > CACHE_SECONDS:
        source = _source_for(ctx.settings, url)
        headers, verify = (auth_headers(source.auth), source.verify_tls) if source else ({}, True)
        final_url, ctype, raw = _download(ctx.settings, url, headers, verify)   # 등록된 사내 사이트면 그 인증·TLS 로
        page = _extract(ctype, raw, final_url)
        swagger = _as_api_docs(ctx.settings, final_url, raw, page, headers, verify)
        _pages[url] = (time.monotonic(), final_url, *(swagger or page))
        while len(_pages) > 30:
            _pages.popitem(last=False)
    _, final_url, title, text, is_html = _pages[url]
    if is_html and len(text.strip()) < MIN_HTML_TEXT and offset == 0:
        # 자바스크립트로 그리는 페이지(SPA)는 HTML 에 본문이 없다. 브라우저 없이 읽을 방법이 없으니 넘어가게 한다.
        return (f"제목: {title or '(없음)'}\n주소: {final_url}\n\n본문을 거의 읽지 못했습니다 ({len(text.strip())}자). "
                "자바스크립트로 그려지는 페이지일 가능성이 큽니다. 검색 결과의 다른 페이지를 읽으세요."
                + (f"\n읽힌 내용: {text.strip()}" if text.strip() else ""))
    chunk = text[offset: offset + PAGE_CHARS]
    end = offset + len(chunk)
    head = f"제목: {title or '(없음)'}\n주소: {final_url}\n"
    if end < len(text):   # QA API-3: 첫 쪽만 읽고 '문서에 없다'·'전체'라고 판단하지 않게
        tail = (f"\n\n(본문 전체 {len(text)}자 중 {offset}-{end}자 — 아직 {end * 100 // len(text)}%까지만 읽음. 이어 읽으려면 "
                f"offset={end}. 문서에 무엇이 없다고 하거나 전체를 정리하려면 끝까지 읽어야 합니다)")
    else:
        tail = f"\n\n(본문 끝, 전체 {len(text)}자)"
    return head + "\n" + (chunk or "(본문이 비어 있습니다)") + tail
