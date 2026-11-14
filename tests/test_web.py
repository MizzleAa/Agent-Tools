import http.server
import threading

import pytest

from agent.tools import ToolError
from agent.tools import web
from agent.tools.web import check_url, fetch_url, web_search

PAGE = """<!doctype html><html><head><title>테스트 문서</title></head><body>
<nav>메뉴 메뉴 메뉴</nav>
<article><h1>사내 위키</h1><p>llama-server 는 OpenAI 호환 API 를 제공한다. 이 문장은 본문이다.
GGUF 모델 파일 하나와 실행 파일 하나만 있으면 폐쇄망에서도 동작한다.</p>
<p>두 번째 문단도 본문이다. 에이전트는 이 내용을 읽을 수 있어야 하고, 메뉴나 광고는 빠져야 한다.</p></article>
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect-internal":
            self.send_response(302)
            self.send_header("Location", "http://10.255.255.1/secret")
            self.end_headers()
            return
        body = PAGE.encode("utf-8") if self.path == "/page" else ("x" * 10000).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8" if self.path == "/page" else "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    web._pages.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/v1/models",     # llama-server
    "http://localhost:8765/api/runs",      # 관리 화면
    "http://10.0.0.5/",
    "http://192.168.0.1/",
    "http://169.254.169.254/latest/meta-data",
    "http://[::1]/",
    "file:///C:/Windows/win.ini",
    "ftp://example.com/x",
])
def test_internal_and_non_http_urls_are_blocked(settings, url):
    with pytest.raises(ToolError):
        check_url(settings, url)


def test_allowlist_permits_internal_host(settings):
    settings.web.allow_private_hosts = ["127.0.0.0/8"]
    check_url(settings, "http://127.0.0.1:9/")
    settings.web.allow_private_hosts = ["localhost"]
    check_url(settings, "http://localhost:9/")


def test_fetch_extracts_main_text(settings, ctx, local_site):
    settings.web.allow_private_hosts = ["127.0.0.1"]
    out = fetch_url(ctx, url=f"{local_site}/page")
    assert "제목: 테스트 문서" in out and "OpenAI 호환 API" in out and "본문 끝" in out


def test_fetch_pages_long_text_with_offset(settings, ctx, local_site):
    settings.web.allow_private_hosts = ["127.0.0.1"]
    first = fetch_url(ctx, url=f"{local_site}/long")
    assert f"offset={web.PAGE_CHARS}" in first
    second = fetch_url(ctx, url=f"{local_site}/long", offset=web.PAGE_CHARS)
    assert "xxx" in second


def test_js_only_page_tells_model_to_move_on(settings, ctx, monkeypatch):
    monkeypatch.setattr(web, "_download", lambda s, u, *a: (u, "text/html", b"<html><title>App</title><div id=root></div></html>"))
    web._pages.clear()
    out = fetch_url(ctx, url="https://spa.example/")
    assert "다른 페이지를 읽으세요" in out


def test_redirect_to_internal_address_is_blocked(settings, ctx, local_site):
    settings.web.allow_private_hosts = ["127.0.0.1"]   # 시작 주소만 허용, 리다이렉트 대상(10.x)은 불허
    with pytest.raises(ToolError, match="차단"):
        fetch_url(ctx, url=f"{local_site}/redirect-internal")


def test_search_disabled_in_airgapped_mode(settings, ctx):
    settings.web.search_provider = "none"
    with pytest.raises(ToolError, match="꺼져"):
        web_search(ctx, query="아무거나")


def _fake(results_by_region, seen_queries=None):
    def search(settings, query, n, region):
        if seen_queries is not None:
            seen_queries[region] = query
        value = results_by_region[region]
        if isinstance(value, Exception):
            raise value
        return [{"title": t, "url": f"https://{u}", "snippet": "요약"} for t, u in value][:n]
    return search


def test_search_merges_korea_and_us_alternately(ctx, monkeypatch):
    monkeypatch.setattr(web, "_search_ddgs", _fake({
        "kr-kr": [("한국1", "kr.example/1"), ("한국2", "kr.example/2"), ("공통", "same.example")],
        "us-en": [("공통", "same.example"), ("US1", "us.example/1"), ("US2", "us.example/2")],
    }))
    out = web_search(ctx, query="llama.cpp")
    lines = [l for l in out.splitlines() if l[:1].isdigit()]
    assert lines[0] == "1. [한국] 한국1" and lines[1] == "2. [미국·한국] 공통" and lines[2] == "3. [한국] 한국2"
    assert out.count("https://same.example") == 1            # 중복 주소는 한 번만, 나온 지역은 모두 표시


def test_us_search_uses_english_query(ctx, monkeypatch):
    seen = {}
    monkeypatch.setattr(web, "_search_ddgs", _fake({"kr-kr": [], "us-en": [("US", "us.example")]}, seen))
    web_search(ctx, query="llama.cpp 사용법", query_en="llama.cpp tutorial")
    assert seen == {"kr-kr": "llama.cpp 사용법", "us-en": "llama.cpp tutorial"}


def test_search_returns_partial_results_when_one_region_fails(ctx, monkeypatch):
    monkeypatch.setattr(web, "_search_ddgs", _fake({"kr-kr": [("한국1", "kr.example/1")], "us-en": TimeoutError()}))
    out = web_search(ctx, query="x")
    assert "[한국] 한국1" in out and "미국 검색 실패" in out


def test_search_raises_when_all_regions_fail(ctx, monkeypatch):
    monkeypatch.setattr(web, "_search_ddgs", _fake({"kr-kr": TimeoutError(), "us-en": TimeoutError()}))
    with pytest.raises(ToolError, match="인터넷 연결"):
        web_search(ctx, query="x")


def test_web_tools_taint_the_run():
    from agent.tools import REGISTRY
    assert REGISTRY["web_search"].taints and REGISTRY["fetch_url"].taints
