"""API 분석·활용 QA(A01~A03) 결함 API-1~API-7 과 3차 E11(V1 정의 변경)을 확인한다.

127.0.0.1 에 가짜 '데이터 API'(OAuth2 password 명세)와 따로 떨어진 '로그인 서버'를 띄운다. 특정 사내 API 의 주소·경로·계정은 쓰지 않는다.
"""

import http.server
import json
import threading
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from agent import apiauth, config, connections, evidence, openapi, standards
from agent.config import load_settings
from agent.events import Note, ToolResult
from agent.loop import Agent, asks_for_credentials
from agent.profiles import load_profile
from agent.tools import ToolError
from agent.tools import web
from agent.tools.admin import add_api
from agent.tools.api import api_get
from agent.tools.shell import package_env
from server import deps
from tests.conftest import FakeLLM, call, reply

USER, PASSWORD = "qa-user", "pa55-w0rd-xyz"


class AuthServer:
    """POST /auth/login (form) → access_token. 토큰은 서버가 '만료'시킬 수 있다."""

    def __init__(self):
        self.logins = 0
        self.valid: set[str] = set()
        me = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                form = dict(urllib.parse.parse_qsl(body))
                ok = self.path == "/auth/login" and "form-urlencoded" in self.headers.get("Content-Type", "") \
                    and form.get("username") == USER and form.get("password") == PASSWORD
                me.logins += ok
                token = f"tok-{me.logins}-abcdef"
                if ok:
                    me.valid.add(token)
                out = {"access_token": token, "token_type": "bearer"} if ok else {"detail": "bad credentials"}
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(out).encode())

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def spec_for(token_url: str) -> dict:
    sec = [{"OAuth2Password": []}]
    return {
        "openapi": "3.1.0", "info": {"title": "SampleDataAPI", "version": "0.1"},
        "paths": {
            "/healthz": {"get": {"summary": "health", "tags": ["health"]}},
            "/datasets": {"get": {"summary": "List datasets", "tags": ["datasets"], "security": sec,
                                  "parameters": [{"name": "page", "in": "query"}]}},
            "/projects": {"get": {"summary": "List projects", "tags": ["projects"], "security": sec}},
            "/reviews": {"get": {"summary": "List reviews", "tags": ["reviews"], "security": sec}},
        },
        "components": {"securitySchemes": {"OAuth2Password": {"type": "oauth2", "flows": {
            "password": {"scopes": {}, "tokenUrl": token_url}}}}},
    }


class DataServer:
    def __init__(self, auth: AuthServer):
        me = self
        self.spec = spec_for(f"{auth.url}/auth/login")
        self.headers: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, code, obj):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

            def do_GET(self):
                if self.path == "/openapi.json":
                    return self._send(200, me.spec)
                if self.path == "/docs":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    return self.wfile.write(b"<html><div id=swagger-ui></div><script>SwaggerUIBundle({url: '/openapi.json'})</script></html>")
                if self.path == "/healthz":
                    return self._send(200, {"ok": True})
                got = self.headers.get("Authorization", "")
                me.headers.append(got)
                if not got.startswith("Bearer ") or got[7:] not in auth.valid:
                    return self._send(401, {"detail": "Not authenticated"})
                return self._send(200, {"total": 7, "items": [{"id": 1, "name": "a", "owner": USER}]})

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def servers():
    auth = AuthServer()
    data = DataServer(auth)
    web._pages.clear()
    yield data, auth
    data.server.shutdown()
    auth.server.shutdown()


@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONNECTIONS_FILE", tmp_path / "connections.yaml")
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "secrets.yaml")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(connections, "_check_rule", lambda rule: None)   # 테스트 서버가 127.0.0.1 이라서
    apiauth._cache.clear()
    return tmp_path


def intranet():
    p = load_profile("intranet")
    p.auto_approve_writes = True
    return p


def all_text(events, agent) -> str:
    return json.dumps([e.to_dict() for e in events], ensure_ascii=False) + json.dumps(agent.messages, ensure_ascii=False)


# ───────────────────────── API-2. 명세의 인증 정의

def test_API2_security_is_in_summary_with_local_token_url_fixed():
    spec = spec_for("http://127.0.0.1:19999/auth/login")
    title, text = openapi.describe(spec, "http://api.example.intra:8080/openapi.json")
    first_page = text[:3500]
    assert "OAuth2 password" in first_page and "http://127.0.0.1:19999/auth/login" in first_page
    assert "localhost" in first_page and "http://api.example.intra:19999/auth/login" in first_page
    assert "인증이 필요한 엔드포인트: 3/4" in first_page and "GET /healthz" in first_page
    assert "기능 그룹(전체): " in first_page and all(g in first_page for g in ("datasets", "projects", "reviews", "health"))


def test_API2_other_schemes():
    spec = spec_for("/x")
    spec["components"]["securitySchemes"] = {"Key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}
    spec["paths"]["/datasets"]["get"]["security"] = [{"Key": []}]
    sec = openapi.security_info(spec, "http://a.intra/openapi.json")
    assert sec["schemes"][0]["how"] == "token" and sec["schemes"][0]["header"] == "X-Api-Key"
    auth = openapi.auth_from_spec(spec, "http://a.intra/openapi.json", "SAMPLE")
    assert auth.kind == "token" and auth.header == "X-Api-Key" and auth.secret == "SAMPLE_TOKEN"
    no_sec = spec_for("/x")
    no_sec.pop("components")
    for p in no_sec["paths"].values():
        p["get"].pop("security", None)
    assert openapi.auth_from_spec(no_sec, "http://a.intra/openapi.json", "S") is None
    assert "문서에 인증 정의가 없음" in openapi.security_lines(openapi.security_info(no_sec, "http://a.intra/x"))[0]


def test_API2_fetch_url_of_raw_spec_shows_auth(settings, ctx, servers):
    data, auth = servers
    settings.web.allow_private_hosts.append("127.0.0.1")
    out = web.fetch_url(ctx, url=f"{data.url}/openapi.json")
    assert "OAuth2 password" in out and f"{auth.url}/auth/login" in out


# ───────────────────────── API-6 · API-1 · API-7. 등록 → 관리 화면에 계정 → 로그인해서 조회

def test_API6_A01_flow_register_then_account_in_ui_then_count(settings, ctx, servers, temp_config):
    data, auth = servers
    out = add_api(ctx, url=f"{data.url}/docs", name="회사 데이터 시스템")
    api = settings.apis[0]
    assert api.name == "회사-데이터-시스템" and api.title == "회사 데이터 시스템"            # API-7
    assert "도구에서 '회사-데이터-시스템' 로 부른다" in out
    assert api.auth.kind == "login" and api.auth.login_url == f"{auth.url}/auth/login"      # API-1: 명세의 인증을 등록에 반영
    assert "아이디·비밀번호" in out and "상태 · 연결" in out
    with pytest.raises(ToolError, match="계정이 저장되지 않았습니다"):                       # 계정 전: 할 일을 정확히 안내
        api_get(ctx, api=api.name, path="/datasets")

    connections.set_api_credentials(settings, api.name, username=USER, password=PASSWORD)   # 관리 화면이 하는 일
    script = [reply(call("api_get", api=api.name, path="/datasets?page=1")), reply(content="데이터셋은 7개입니다.")]
    agent = Agent(settings, intranet(), llm=FakeLLM(script), approver=lambda r: True)
    events = list(agent.run("데이터셋 몇 개야?"))
    result = next(e for e in events if isinstance(e, ToolResult))
    assert "HTTP 200" in result.output and '"total": 7' in result.output and auth.logins == 1
    everything = all_text(events, agent)
    assert PASSWORD not in everything and "tok-1-abcdef" not in everything and USER not in everything   # 계정·토큰은 모델·기록에 없음
    assert "***" in result.output                                                                    # 응답에 섞인 아이디도 가림


def test_API6_expired_token_logs_in_again(settings, ctx, servers):
    data, auth = servers
    connections.add_api_from_docs(settings, f"{data.url}/docs", name="data")
    connections.set_api_credentials(settings, "data", username=USER, password=PASSWORD)
    assert "HTTP 200" in api_get(ctx, api="data", path="/datasets")
    auth.valid.clear()                                                    # 서버가 토큰을 만료시킴
    out = api_get(ctx, api="data", path="/datasets")
    assert "HTTP 200" in out and auth.logins == 2


def test_API6_wrong_password_and_unreachable_login_are_explained(settings, ctx, servers):
    data, auth = servers
    connections.add_api_from_docs(settings, f"{data.url}/docs", name="data")
    connections.set_api_credentials(settings, "data", username=USER, password="nope")
    with pytest.raises(ToolError) as e:
        api_get(ctx, api="data", path="/datasets")
    assert "로그인이 거절됐습니다" in str(e.value) and "nope" not in str(e.value)
    connections.set_api_auth(settings, "data", "login", login_url="http://127.0.0.1:1/auth/login")
    connections.set_api_credentials(settings, "data", username=USER, password=PASSWORD)
    with pytest.raises(ToolError, match="접속하지 못했습니다"):
        api_get(ctx, api="data", path="/datasets")


def test_API1_401_without_auth_explains_the_real_next_step(settings, ctx, servers):
    data, _ = servers
    connections.add_api(settings, "plain", data.url)                     # 인증 없이 등록된 API
    out = api_get(ctx, api="plain", path="/datasets")
    assert "HTTP 401" in out and "인증 설정이 없습니다" in out and "상태 · 연결" in out and "변경 요청 허용" not in out


def test_API_ui_endpoints_set_auth_credentials_and_check(monkeypatch, settings, temp_config, servers):
    data, auth = servers
    (temp_config / "settings.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(deps, "load_settings", lambda: load_settings(temp_config / "settings.yaml").model_copy(
        update={"workspace": settings.workspace, "data_dir": settings.data_dir}))
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    h = {"X-Agent-Token": deps.TOKEN}
    assert c.post("/api/v1/apis", json={"url": f"{data.url}/docs", "name": "data"}, headers=h).status_code == 200
    info = next(a for a in c.get("/api/v1/status").json()["intranet"]["apis"] if a["name"] == "data")
    assert info["auth_kind"] == "login" and info["auth"] is False and info["login_url"] == f"{auth.url}/auth/login"
    r = c.put("/api/v1/apis/data/credentials", json={"username": USER, "password": PASSWORD}, headers=h)
    assert r.status_code == 200 and PASSWORD not in r.text
    check = c.post("/api/v1/apis/data/login-check", headers=h).json()
    assert check["ok"] and "tok-" not in json.dumps(check)
    status = c.get("/api/v1/status").text
    assert PASSWORD not in status and USER not in status
    info = next(a for a in c.get("/api/v1/status").json()["intranet"]["apis"] if a["name"] == "data")
    assert info["auth"] is True
    assert c.put("/api/v1/apis/data/auth", json={"kind": "token", "header": "Authorization", "prefix": "Bearer "},
                 headers=h).status_code == 200
    info = next(a for a in c.get("/api/v1/status").json()["intranet"]["apis"] if a["name"] == "data")
    assert info["auth_kind"] == "token" and info["auth"] is False


def test_API_secret_names_are_ascii_even_for_korean_names():
    assert connections.secret_base("SampleDataAPI", "http://x:1") == "SAMPLEDATAAPI"
    assert connections.secret_base("회사 데이터", "http://10.1.2.3:20000") == "API_10_1_2_3_20000"
    assert connections.api_name("DLThing API") == "dlthing-api" and connections.api_name("회사 데이터 시스템") == "회사-데이터-시스템"


# ───────────────────────── API-4 · API-5. 채팅으로 비밀번호를 요구·수용하지 않음

@pytest.mark.parametrize("text,asks", [
    ("아이디(username)와 비밀번호(password)를 알려주세요. 알려주시면 즉시 로그인하겠습니다!", True),
    ("Please send me your password and I will log in.", True),
    ("토큰을 보내 주시면 바로 조회하겠습니다.", True),
    ("관리 화면 '상태 · 연결' › 사내 API 의 인증 칸에 아이디·비밀번호를 넣어 주세요.", False),
    ("데이터셋은 7개입니다.", False),
    ("남은 토큰 수를 알려 드릴게요.", False),
    # eval find_todo 에서 실제로 잘못 걸렸던 답 (코드 TODO 의 '비밀번호' + 끝인사의 '주세요')
    ("**TODO 내용:** `비밀번호 재시도 횟수 제한 추가` (4 번째 줄). 더 필요한 게 있으면 말씀해 주세요.", False),
    ("TODO: 비밀번호 재시도 횟수 제한 추가 — 필요하면 추가해 주세요.", False),
    ("로그인하려면 비밀번호를 채팅으로 보내 주세요.", True),
])
def test_API4_detects_credential_requests(text, asks):
    assert asks_for_credentials(text) is asks


def test_API4_guard_is_only_for_network_profiles(settings, ws):
    answer = "아이디와 비밀번호를 알려주세요."   # 코드 작업 용도(웹·API 도구 없음)는 이 점검을 하지 않는다
    events = list(Agent(settings, load_profile("general"), llm=FakeLLM([reply(content=answer)])).run("질문"))
    assert not [e for e in events if isinstance(e, Note)] and events[-1].answer == answer


def test_API4_answer_asking_password_is_sent_back(settings, servers):
    ask = "아이디와 비밀번호를 알려주세요. 알려주시면 즉시 로그인하겠습니다."
    fixed = "로그인이 필요합니다. 관리 화면 '상태 · 연결' › 사내 API 의 인증 칸에 계정을 넣어 주세요."
    agent = Agent(settings, intranet(), llm=FakeLLM([reply(content=ask), reply(content=fixed)]))
    events = list(agent.run("데이터셋 몇 개야?"))
    assert [n.kind for n in events if isinstance(n, Note)] == ["credentials"]
    assert events[-1].answer == fixed
    stubborn = Agent(settings, intranet(), llm=FakeLLM([reply(content=ask)] * 2))
    final = list(stubborn.run("데이터셋 몇 개야?"))[-1]
    assert "채팅으로 보내지 마세요" in final.answer


@pytest.mark.parametrize("typed,hidden", [
    ("로그인해줘. 아이디는 kim01 비밀번호는 Secr3t!x", 2),
    ("token: eyJhbGciOiJIUzI1NiJ9.abc", 1),
    ("password=hunter22 로 해봐", 1),
    ("토큰은 3개 남았어", 0),
    ("비밀번호는 관리 화면에 넣었어", 0),
    ("데이터셋 id 는 42", 0),
])
def test_API4_typed_credentials_are_masked(typed, hidden):
    masked, n = apiauth.mask_credentials(typed)
    assert n == hidden
    for secret in ("kim01", "Secr3t!x", "eyJhbGciOiJIUzI1NiJ9.abc", "hunter22"):
        if secret in typed and hidden:
            assert secret not in masked


def test_API4_server_masks_before_model_and_record(monkeypatch, settings, tmp_path):
    from agent.store import Store
    seen = []

    class Spy(FakeLLM):
        def chat(self, messages, tools=None, think=None):
            seen.append(json.dumps(messages, ensure_ascii=False))
            return super().chat(messages, tools, think)

    monkeypatch.setattr(deps, "store", Store(tmp_path / "t.db"))
    monkeypatch.setattr(deps, "load_settings", lambda: settings)
    monkeypatch.setattr(deps, "runs", {})
    monkeypatch.setattr(deps, "Agent", lambda s, p, approver, **kw: Agent(s, p, llm=Spy([reply(content="관리 화면에 넣어 주세요.")]),
                                                                        approver=approver, **kw))
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    from tests.test_server import wait_done
    rid = c.post("/api/v1/runs", json={"task": "로그인 해줘. 비밀번호는 Secr3t!x", "profile": "general"},
                 headers={"X-Agent-Token": deps.TOKEN}).json()["run_id"]
    run = wait_done(c, rid)
    assert "Secr3t!x" not in json.dumps(run, ensure_ascii=False) and "Secr3t!x" not in "".join(seen)
    assert "가리고 저장하지 않았습니다" in run["task"]


def test_API5_context_shows_auth_state_so_promises_match_reality(settings, servers):
    data, _ = servers
    connections.add_api_from_docs(settings, f"{data.url}/docs", name="data")
    prompt = Agent(settings, intranet(), llm=FakeLLM([])).system_prompt()
    assert "인증: 로그인(OAuth2 password)" in prompt and "아직 계정 없음" in prompt
    connections.set_api_credentials(settings, "data", username=USER, password=PASSWORD)
    prompt = Agent(settings, intranet(), llm=FakeLLM([])).system_prompt()
    assert "계정 저장됨" in prompt and PASSWORD not in prompt and USER not in prompt


# ───────────────────────── API-3. 긴 문서의 일부만 읽음

def test_API3_partial_read_is_marked(ctx, monkeypatch):
    long = "가" * 9000
    monkeypatch.setattr(web, "_download", lambda *a, **k: ("http://x/doc", "text/plain; charset=utf-8", long.encode()))
    web._pages.clear()
    out = web.fetch_url(ctx, url="http://x/doc")
    assert "아직 " in out and "%까지만 읽음" in out and "끝까지 읽어야" in out


# ───────────────────────── E11. V1 = ① 문법 ② 정의되지 않은 이름 ③ 모듈 로드

def test_E11_standards_copy_matches_qa_original():
    qa = Path(r"C:\workspace\Agent_QA\기준")
    if not qa.is_dir():
        pytest.skip("QA 기준 폴더 없음")
    # 줄 끝(LF/CRLF)은 git 이 받을 때 바꿀 수 있으므로 (core.autocrlf) 내용만 비교한다
    def text(p: Path) -> bytes:
        return p.read_bytes().replace(b"\r\n", b"\n")
    ours = {p.name: text(p) for p in standards.STANDARDS_DIR.glob("*.md")}
    theirs = {p.name: text(p) for p in qa.glob("*.md")}
    assert ours == theirs


def _v1(answer, ledger, tmp_path):
    return evidence.review(answer, "t", ledger, tmp_path, used_any_tool=True, dev_report=True)


def test_E11_v1_claim_needs_undefined_names_and_module_load(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "requirements.txt").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text("from lib import Missing\n", encoding="utf-8")
    report = "## 결과: 부분 완료\n| # | 요구 | 구현 위치 | 등급 | 근거 |\n|---|---|---|---|---|\n| 1 | 시작 | app/main.py | V1 | compileall |\n"
    syntax = [evidence.Step("run_command", {"command": ".venv\\Scripts\\python -m compileall -q app"}, True, "exit_code: 0")]
    rv = _v1(report, syntax, tmp_path)
    assert any("② 정의되지 않은 이름" in f.text and "③ 모듈 로드" in f.text for f in rv.blocking)
    load_fail = syntax + [evidence.Step("run_command", {"command": ".venv\\Scripts\\python -m pyflakes app"}, True, "exit_code: 0"),
                          evidence.Step("run_command", {"command": "cd app; .venv\\Scripts\\python -c \"import main\""}, True,
                                        "exit_code: 1\nImportError: cannot import name 'Missing' from 'lib'")]
    assert any("③ 모듈 로드(import)가 실패" in f.text for f in _v1(report, load_fail, tmp_path).blocking)
    ok = load_fail[:2] + [evidence.Step("run_python", {"code": "import importlib, pkgutil\nimport main"}, True, "exit_code: 0")]
    assert not [f for f in _v1(report, ok, tmp_path).findings if "V1" in f.text]


def test_E11_cut_off_v1_claim_like_R15_is_caught(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "pyproject.toml").write_text("", encoding="utf-8")
    syntax = [evidence.Step("run_command", {"command": "python -m compileall -q main.py"}, True, "exit_code: 0")]
    rv = _v1("V1 정적 검증이 통과했습니다. 이제 V2 단", syntax, tmp_path)
    assert any("V1 은 ① 문법" in f.text for f in rv.blocking)


def test_E11_syntax_only_command_gets_v1_hint(settings, ws):
    p = load_profile("coder")
    p.auto_approve_writes = True
    script = [reply(call("run_python", code="import compileall; compileall.compile_dir('.', quiet=1)")),
              reply(call("run_python", code="import compileall")), reply(content="끝")]
    events = list(Agent(settings, p, llm=FakeLLM(script), approver=lambda r: True).run("t"))
    outs = [e.output for e in events if isinstance(e, ToolResult)]
    assert "V1 ① 문법만" in outs[0] and "V1 ① 문법만" not in outs[1]


def test_E11_offline_packages_env(tmp_path):
    assert package_env(tmp_path) == {}
    (tmp_path / "python").mkdir()
    (tmp_path / "npm-cache").mkdir()
    online = package_env(tmp_path)
    assert online["PIP_FIND_LINKS"].endswith("python") and "PIP_NO_INDEX" not in online and online["npm_config_prefer_offline"] == "true"
    (tmp_path / ".offline").touch()
    off = package_env(tmp_path)
    assert off["PIP_NO_INDEX"] == "1" and off["npm_config_offline"] == "true"
