"""사내 사이트 수집·검색, 사내 API 도구 테스트. 127.0.0.1 에 가짜 사내 서버(위키 + API)를 띄워서 검증한다."""

import http.server
import json
import threading

import pytest

from agent.config import ApiEndpoint, Auth, IntranetSource
from agent.crawler import crawl
from agent.events import ApprovalRequested, ToolResult
from agent.index import DocIndex, chunk_text
from agent.loop import Agent
from agent.tools import REGISTRY, ToolError
from agent.tools import web
from agent.tools.api import api_get, api_send, build_url
from agent.tools.intranet import search_intranet
from agent.tools.web import check_url
from tests.conftest import FakeLLM, call, reply

PAGES = {
    "/wiki/": '<html><title>위키 홈</title><body><p>사내 위키 첫 화면입니다. 아래 문서를 참고하세요. 공정 매뉴얼과 장비 사용법이 있습니다.</p>'
              '<a href="/wiki/a">설비</a> <a href="b">로트</a> <a href="/logout">로그아웃</a> '
              '<a href="/other/x">범위 밖</a> <a href="http://example.com/">외부</a> <a href="/wiki/logo.png">이미지</a></body></html>',
    "/wiki/a": '<html><title>설비 관리</title><body><article><p>생산설비 점검 주기는 3개월이다. 점검 결과는 MES 에 기록한다. '
               '설비 이상 시 즉시 라인 담당자에게 알린다. 이 문서는 설비 관리 매뉴얼의 요약본이다.</p>'
               '<a href="/wiki/b#top">로트</a></article></body></html>',
    "/wiki/b": '<html><title>로트 추적</title><body><article><p>로트 번호로 공정 이력을 추적하는 방법을 설명한다. '
               '로트 상세는 MES 의 로트 조회 화면이나 API 로 확인할 수 있다. 추적 결과는 품질팀과 공유한다.</p></article></body></html>',
}


class Site:
    def __init__(self):
        self.pages = dict(PAGES)
        self.hits: list[str] = []
        self.headers: list[dict] = []
        self.posts: list[bytes] = []
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def do_GET(self):
                site.hits.append(self.path)
                site.headers.append(dict(self.headers))
                if self.path in site.pages:
                    return self._send(200, site.pages[self.path])
                if self.path.startswith("/api/lots/"):
                    if self.headers.get("Authorization") != "Bearer secret123":
                        return self._send(401, '{"error":"unauthorized"}', "application/json")
                    lot = self.path.rsplit("/", 1)[1]
                    return self._send(200, json.dumps({"lot": lot, "status": "RUN", "echo": "secret123"}), "application/json")
                if self.path == "/redir":
                    return self._send(302, "", extra={"Location": site.other_url + "/landing"})
                if self.path == "/landing":
                    return self._send(200, "<html><body>landing</body></html>")
                return self._send(404, "not found")

            def do_POST(self):
                site.posts.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                return self._send(201, '{"ok": true}', "application/json")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.other_url = ""
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def site():
    s = Site()
    other = Site()            # 다른 포트 = 다른 호스트 (리다이렉트 테스트용)
    s.other_url = other.url
    s.other = other
    web._pages.clear()
    yield s
    s.server.shutdown()
    other.server.shutdown()


@pytest.fixture
def intranet_settings(settings, site, monkeypatch):
    monkeypatch.setenv("MES_TOKEN", "secret123")
    settings.intranet.sources = [IntranetSource(
        name="wiki", description="사내 위키", start_urls=[f"{site.url}/wiki/"],
        include_prefixes=[f"{site.url}/wiki/"], exclude_patterns=["/logout"], delay=0)]
    settings.apis = [ApiEndpoint(name="mes", description="생산 실행 시스템", base_url=f"{site.url}/api",
                                 auth=Auth(secret="MES_TOKEN", prefix="Bearer "), allow_methods=["GET"],
                                 docs="GET /lots/{lot_id}  로트 상세")]
    return settings


# ───────────────────────── 색인

def test_chunk_text_respects_size():
    chunks = chunk_text("가" * 3000 + "\n\n" + "짧은 문단\n다음 줄", size=1200)
    assert all(len(c) <= 1200 for c in chunks) and chunks[-1].endswith("다음 줄")


def test_korean_search_short_and_long_terms(tmp_path):
    idx = DocIndex(tmp_path / "i.db")
    idx.upsert("wiki", "http://w/a", "설비 관리", "생산설비 점검 주기는 3개월이다.")
    idx.upsert("wiki", "http://w/b", "로트 추적", "로트 번호로 공정 이력을 추적한다.")
    assert idx.search("설비")[0]["url"] == "http://w/a"            # 2글자 → LIKE
    assert idx.search("산설비")[0]["url"] == "http://w/a"          # 단어 중간 3글자 → trigram
    assert idx.search("로트 이력")[0]["url"] == "http://w/b"
    assert idx.search("없는단어조합") == []
    assert idx.upsert("wiki", "http://w/a", "설비 관리", "생산설비 점검 주기는 3개월이다.") is False   # 그대로면 재색인 안 함
    assert idx.remove_missing("wiki", {"http://w/a"}) == 1 and idx.count() == 1


# ───────────────────────── 수집

def test_crawl_stays_in_scope_and_skips_excluded(intranet_settings, site):
    st = crawl(intranet_settings, intranet_settings.intranet.sources[0])
    assert st.state == "done" and st.pages == 3 and st.errors == 0
    assert "/logout" not in site.hits and "/other/x" not in site.hits and "/wiki/logo.png" not in site.hits


def test_crawl_removes_deleted_pages_on_full_run(intranet_settings, site):
    src = intranet_settings.intranet.sources[0]
    crawl(intranet_settings, src)
    del site.pages["/wiki/b"]
    st = crawl(intranet_settings, src)
    assert st.removed == 1 and st.changed == 0


def test_search_intranet_tool_after_crawl(intranet_settings, ctx):
    crawl(intranet_settings, intranet_settings.intranet.sources[0])
    out = search_intranet(ctx, query="점검 주기")
    assert out.startswith("1. [wiki] 설비 관리") and "/wiki/a" in out


def test_search_intranet_empty_index_tells_what_to_do(ctx):
    with pytest.raises(ToolError, match="지금 수집"):
        search_intranet(ctx, query="아무거나")


# ───────────────────────── 사내 API

def test_api_get_uses_secret_and_hides_it(intranet_settings, ctx, site):
    out = api_get(ctx, api="mes", path="/lots/L123")
    assert "HTTP 200" in out and '"status": "RUN"' in out
    assert "secret123" not in out and "***" in out                       # 응답에 토큰이 섞여 와도 가린다
    assert site.headers[-1]["Authorization"] == "Bearer secret123"


@pytest.mark.parametrize("path", ["//evil.com/x", "http://evil.com/", "/../admin", "/%2e%2e/admin", "lots/1", "/x\\y", "/@evil"])
def test_api_path_cannot_escape_base(intranet_settings, path):
    with pytest.raises(ToolError):
        build_url(intranet_settings.apis[0], path)


def test_api_rules(intranet_settings, ctx, monkeypatch):
    with pytest.raises(ToolError, match="등록되지 않은"):
        api_get(ctx, api="erp", path="/x")
    with pytest.raises(ToolError, match="허용되지 않습니다"):
        api_send(ctx, api="mes", method="POST", path="/lots", body="{}")
    monkeypatch.delenv("MES_TOKEN")
    with pytest.raises(ToolError, match="MES_TOKEN"):
        api_get(ctx, api="mes", path="/lots/1")


def test_api_send_always_needs_approval(intranet_settings, profile, site):
    intranet_settings.apis[0].allow_methods = ["GET", "POST"]
    profile.tools = ["api_get", "api_send"]
    profile.auto_approve_writes = True                      # 파일 자동 승인이 켜져 있어도 API 변경은 묻는다
    llm = FakeLLM([reply(call("api_send", api="mes", method="POST", path="/lots", body='{"qty": 3,}')), reply(content="끝")])
    asked = []
    events = list(Agent(intranet_settings, profile, llm=llm, approver=lambda r: asked.append(r) or False).run("생성"))
    assert asked and "API" in asked[0].reason and site.posts == []
    assert "거부" in [e for e in events if isinstance(e, ToolResult)][0].output
    assert REGISTRY["api_send"].permission == "send"


def test_api_send_approved_sends_repaired_json(intranet_settings, profile, site):
    intranet_settings.apis[0].allow_methods = ["GET", "POST"]
    profile.tools = ["api_send"]
    llm = FakeLLM([reply(call("api_send", api="mes", method="POST", path="/lots", body='{"qty": 3,}')), reply(content="끝")])
    list(Agent(intranet_settings, profile, llm=llm, approver=lambda r: True).run("생성"))
    assert json.loads(site.posts[0]) == {"qty": 3}


def test_system_prompt_lists_apis_and_sources(intranet_settings, profile):
    profile.tools = ["api_get", "search_intranet"]
    prompt = Agent(intranet_settings, profile, llm=FakeLLM([])).system_prompt()
    assert "- mes: 생산 실행 시스템" in prompt and "GET /lots/{lot_id}" in prompt and "- wiki: 사내 위키" in prompt


def test_registered_hosts_are_allowed_but_others_stay_blocked(intranet_settings, site):
    check_url(intranet_settings, f"{site.url}/wiki/")
    with pytest.raises(ToolError):
        check_url(intranet_settings, "http://10.1.2.3/")


def test_auth_header_not_forwarded_to_other_host_on_redirect(intranet_settings, site):
    web._download(intranet_settings, f"{site.url}/redir", {"Authorization": "Bearer secret123"})
    assert site.headers[-1].get("Authorization") == "Bearer secret123"     # 처음 호스트에는 보냄
    assert "Authorization" not in site.other.headers[-1]                   # 리다이렉트된 다른 호스트에는 안 보냄
