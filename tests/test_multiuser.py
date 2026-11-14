"""로그인 사용 모드 (여러 고객사가 서버 한 대에 접속): 로그인 · 계정 발급 · 30일 변경 · 비밀번호 찾기 · 권한 ·
사용자별 분리(대화 · 작업 폴더 · 이미지 · 승인) · 회사별 용도 · 로컬 모델 차례 · 외부 모델 실행 · 기록·사용량 · 접속 주소.
요구서 Agent_QA/기능요구_계정연동_외부모델.md 의 완료 기준(T2·T5~T13)을 서버 API 로 확인한다."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from agent import accounts, apiauth, config, providers
from agent.config import ApiEndpoint, Auth
from agent.loop import Agent
from agent.store import Store
from server import deps
from tests.conftest import FakeLLM, call, reply

H = {"X-Agent-Token": deps.TOKEN}


def wait_done(c, run_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = c.get(f"/api/v1/runs/{run_id}").json()
        if run.get("status") not in (None, "running"):
            return run
        time.sleep(0.05)
    raise AssertionError("실행이 끝나지 않음")


@pytest.fixture
def env(monkeypatch, settings, tmp_path):
    """서버 + 가짜 모델. agents 에 실행마다 쓰인 설정을 남긴다 (작업 폴더·모델 확인용)."""
    script: list = []
    agents: list = []
    monkeypatch.setattr(config, "CONNECTIONS_FILE", tmp_path / "connections.yaml")
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "secrets.yaml")
    monkeypatch.setattr(deps, "store", Store(tmp_path / "test.db"))
    monkeypatch.setattr(deps, "load_settings", lambda: settings)
    monkeypatch.setattr(deps, "runs", {})
    monkeypatch.setattr(deps, "local_queue", deps.LocalQueue())

    def make(s, p, approver, **kw):
        agents.append(s)
        return Agent(s, p, llm=FakeLLM(script), approver=approver, **kw)

    monkeypatch.setattr(deps, "Agent", make)
    return script, agents


def client() -> TestClient:
    return TestClient(srv.app, base_url="http://127.0.0.1")


def login(username, password) -> TestClient:
    c = client()
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password}, headers=H)
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def admin(env):
    """혼자 쓰기에서 '로그인 사용 켜기' → 첫 관리자."""
    c = client()
    r = c.post("/api/v1/auth/setup", json={"username": "boss", "password": "Adm1nPass!"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["login_enabled"] and r.json()["user"]["role"] == "admin"
    return c


def issue(admin_client, username, company_id="", role="user", password="UserPass!1") -> TestClient:
    """계정 발급 → 임시 비밀번호로 로그인 → 새 비밀번호로 바꾼 클라이언트."""
    r = admin_client.post("/api/v1/admin/users", json={"username": username, "company_id": company_id, "role": role}, headers=H)
    assert r.status_code == 200, r.text
    temp = r.json()["temp_password"]
    c = login(username, temp)
    assert c.get("/api/v1/auth/me").json()["user"]["must_change"]
    assert c.post("/api/v1/auth/password", json={"current_password": temp, "new_password": password},
                  headers=H).status_code == 200
    return c


def company(admin_client, name, profiles=(), seats=None) -> str:
    r = admin_client.post("/api/v1/admin/companies", json={"name": name, "profiles": list(profiles), "seats": seats}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ───────────────────────── 혼자 쓰기 (기본)

def test_default_is_single_user_without_login(env):
    c = client()
    me = c.get("/api/v1/auth/me").json()
    assert not me["login_enabled"] and me["user"] is None and not me["has_admin"]
    assert c.get("/api/v1/runs").status_code == 200 and c.get("/api/v1/admin/users").status_code == 200
    st = c.get("/api/v1/status").json()
    assert st["access"] == {"login_enabled": False, "offline": False, "is_admin": True}


def test_setup_assigns_earlier_runs_to_first_admin(env):
    script, _ = env
    c = client()
    script.append(reply(content="예전 작업"))
    rid = c.post("/api/v1/runs", json={"task": "예전", "profile": "general"}, headers=H).json()["run_id"]
    wait_done(c, rid)
    c.post("/api/v1/auth/setup", json={"username": "boss", "password": "Adm1nPass!"}, headers=H)
    assert [r["id"] for r in c.get("/api/v1/runs").json()] == [rid]          # 관리자 것으로 넘어옴
    assert client().get("/api/v1/runs").status_code == 401                    # 로그인 안 한 사람은 못 봄
    assert c.post("/api/v1/auth/setup", json={"username": "x", "password": "Adm1nPass!"}, headers=H).status_code == 409


# ───────────────────────── 로그인 · 비밀번호

def test_login_required_and_temp_password_blocks_until_changed(admin):
    anon = client()
    assert anon.get("/api/v1/runs").status_code == 401
    assert anon.get("/api/v1/auth/me").status_code == 200                     # 로그인 화면은 열린다
    temp = admin.post("/api/v1/admin/users", json={"username": "kim"}, headers=H).json()["temp_password"]
    assert client().post("/api/v1/auth/login", json={"username": "kim", "password": "wrong"}, headers=H).status_code == 401
    c = login("kim", temp)
    assert c.get("/api/v1/runs").status_code == 403                           # 임시 비밀번호 — 바꾸기 전에는 못 씀
    assert c.post("/api/v1/auth/password", json={"current_password": temp, "new_password": "short"},
                  headers=H).status_code == 400
    assert c.post("/api/v1/auth/password", json={"current_password": temp, "new_password": "KimPass!123"},
                  headers=H).status_code == 200
    assert c.get("/api/v1/runs").status_code == 200
    c.post("/api/v1/auth/logout", headers=H)
    assert c.get("/api/v1/runs").status_code == 401


def test_password_older_than_30_days_is_flagged(admin):
    c = issue(admin, "lee")
    uid = c.get("/api/v1/auth/me").json()["user"]["id"]
    old = (datetime.now() - timedelta(days=31)).isoformat(timespec="seconds")
    with deps.accounts.lock, deps.accounts.db:
        deps.accounts.db.execute("update users set password_changed_at=? where id=?", (old, uid))
    me = c.get("/api/v1/auth/me").json()
    assert me["user"]["password_expired"] and me["password_max_age_days"] == 30
    assert c.get("/api/v1/runs").status_code == 200                           # '나중에' — 막지는 않는다
    c.post("/api/v1/auth/password", json={"current_password": "UserPass!1", "new_password": "UserPass!2"}, headers=H)
    assert not c.get("/api/v1/auth/me").json()["user"]["password_expired"]


def test_forgot_password_goes_to_admin(admin):
    c = issue(admin, "park")
    anon = client()
    assert anon.post("/api/v1/auth/reset-requests", json={"username": "park", "company": "A사", "note": "010"},
                     headers=H).status_code == 200
    assert anon.post("/api/v1/auth/reset-requests", json={"username": "ghost"}, headers=H).status_code == 200
    pending = admin.get("/api/v1/admin/reset-requests?pending=true").json()
    req = next(r for r in pending if r["username"] == "park")
    assert req["company"] == "A사" and req["user_id"]
    temp = admin.post(f"/api/v1/admin/users/{req['user_id']}/reset-password", headers=H).json()["temp_password"]
    assert c.get("/api/v1/runs").status_code == 401                           # 재설정하면 열린 로그인은 끝
    assert client().post("/api/v1/auth/login", json={"username": "park", "password": "UserPass!1"},
                         headers=H).status_code == 401
    again = login("park", temp)
    assert again.get("/api/v1/auth/me").json()["user"]["must_change"]
    assert {r["username"] for r in admin.get("/api/v1/admin/reset-requests?pending=true").json()} == {"ghost"}


# ───────────────────────── 권한 · 분리

def test_users_cannot_use_admin_apis(admin):
    c = issue(admin, "choi")
    for method, path in [("get", "/api/v1/admin/users"), ("get", "/api/v1/settings"), ("get", "/api/v1/admin/providers"),
                         ("post", "/api/v1/apis"), ("put", "/api/v1/models/active"), ("post", "/api/v1/runtime/stop"),
                         ("put", "/api/v1/admin/access"), ("get", "/api/v1/admin/runs"), ("post", "/api/v1/hosts")]:
        r = getattr(c, method)(path, headers=H, **({} if method == "get" else {"json": {}}))
        assert r.status_code == 403, (path, r.status_code)
    assert c.get("/api/v1/status").json()["access"] == {"login_enabled": True, "offline": False, "is_admin": False}


def test_runs_and_folders_are_separated(env, admin, settings):
    script, agents = env
    settings.session_folders = True   # 실제 운영처럼 대화마다 작업 폴더
    a, b = issue(admin, "user_a"), issue(admin, "user_b")
    script.append(reply(call("write_file", path="a.txt", content="A의 파일")))
    script.append(reply(content="저장했습니다"))
    rid = a.post("/api/v1/runs", json={"task": "a.txt 저장", "profile": "general", "auto_approve": "all"},
                 headers=H).json()["run_id"]
    run = wait_done(a, rid)
    assert run["user_id"] and run["provider"] == "local"
    ws_a = settings.workspace_path / "users" / "user_a"
    assert agents[-1].workspace_path == ws_a and list(ws_a.rglob("a.txt"))   # A 의 작업 폴더 안에만
    assert all(str(d.path).startswith(str(ws_a)) for d in agents[-1].allowed_dirs)
    # B 는 A 의 실행·대화·이벤트·폴더를 볼 수 없다 (있는지도 알 수 없게 404)
    assert [r["id"] for r in b.get("/api/v1/runs").json()] == []
    for path in (f"/api/v1/runs/{rid}", f"/api/v1/threads/{rid}", f"/api/v1/runs/{rid}/events",
                 f"/api/v1/runs/{rid}/images/1/thumb"):
        assert b.get(path).status_code == 404, path
    assert b.delete(f"/api/v1/threads/{rid}", headers=H).status_code == 404
    assert b.post("/api/v1/runs", json={"task": "이어서", "parent_run_id": rid}, headers=H).status_code == 404
    assert b.get("/api/v1/workspace/folders").json() == []
    assert len(a.get("/api/v1/workspace/folders").json()) == 1
    # 관리자는 전부 본다 (기록 보기)
    assert admin.get(f"/api/v1/runs/{rid}").status_code == 200
    assert [r["id"] for r in admin.get(f"/api/v1/threads/{rid}").json()] == [rid]
    page = admin.get("/api/v1/admin/runs").json()
    assert page["total"] == 1 and page["items"][0]["user_name"] == "user_a"


def test_only_the_owner_answers_approvals_and_cancels(env, admin):
    script, _ = env
    a, b = issue(admin, "own_a"), issue(admin, "own_b")
    script.append(reply(call("write_file", path="x.txt", content="x")))
    rid = a.post("/api/v1/runs", json={"task": "x 저장", "profile": "general"}, headers=H).json()["run_id"]
    deadline = time.time() + 5
    while time.time() < deadline and not deps.runs[rid].pending:
        time.sleep(0.05)
    call_id = next(iter(deps.runs[rid].pending))
    for other in (b, admin):
        assert other.post(f"/api/v1/runs/{rid}/approvals", json={"call_id": call_id, "approved": True},
                          headers=H).status_code == 404
        assert other.post(f"/api/v1/runs/{rid}/cancel", headers=H).status_code == 404
    assert a.post(f"/api/v1/runs/{rid}/cancel", headers=H).status_code == 200
    assert wait_done(a, rid)["status"] == "cancelled"


def test_company_profiles_limit_what_users_run(env, admin):
    cid = company(admin, "A사", profiles=["general"])
    c = issue(admin, "a_user", company_id=cid)
    assert [p["name"] for p in c.get("/api/v1/profiles").json()] == ["general"]
    r = c.post("/api/v1/runs", json={"task": "코드 짜줘", "profile": "coder"}, headers=H)
    assert r.status_code == 403 and "허용되지 않았" in r.json()["detail"]
    assert len(admin.get("/api/v1/profiles").json()) > 1                      # 관리자는 전부


def test_company_seats_are_enforced_when_issuing(admin):
    cid = company(admin, "B사", seats=1)
    issue(admin, "b1", company_id=cid)
    r = admin.post("/api/v1/admin/users", json={"username": "b2", "company_id": cid}, headers=H)
    assert r.status_code == 400 and "허용 계정 수" in r.json()["detail"]
    assert next(x for x in admin.get("/api/v1/admin/companies").json() if x["id"] == cid)["issued"] == 1


def test_admin_only_chat_tools_are_blocked_for_users(env, admin):
    script, _ = env
    c = issue(admin, "tool_user")
    script.append(reply(call("add_api", url="http://10.0.0.9/docs")))
    script.append(reply(content="관리자에게 요청하세요"))
    rid = c.post("/api/v1/runs", json={"task": "API 추가해줘", "profile": "intranet", "auto_approve": "all"},
                 headers=H).json()["run_id"]
    run = wait_done(c, rid)
    result = next(e for e in run["events"] if e["type"] == "ToolResult")
    assert not result["ok"] and "관리자만" in result["output"]


# ───────────────────────── 사내 API 본인 계정

def test_internal_api_uses_each_users_own_account(env, admin, settings):
    settings.apis = [ApiEndpoint(name="mes", base_url="http://10.0.0.9", docs="GET /x",
                                 auth=Auth(kind="login", login_url="http://10.0.0.9/login",
                                           username_secret="MES_USERNAME", password_secret="MES_PASSWORD"))]
    config_secret = config.SECRETS_FILE
    config_secret.write_text("MES_USERNAME: shared\nMES_PASSWORD: sharedpw\n", encoding="utf-8")   # 공용 계정은 쓰지 않는다
    a, b = issue(admin, "api_a"), issue(admin, "api_b")
    assert a.get("/api/v1/me/api-accounts").json() == [{"name": "mes", "title": "mes", "base_url": "http://10.0.0.9",
                                                        "has_account": False}]
    assert a.put("/api/v1/me/api-accounts/mes", json={"username": "kim", "password": "kimpw"}, headers=H).status_code == 200
    assert a.get("/api/v1/me/api-accounts").json()[0]["has_account"]
    assert not b.get("/api/v1/me/api-accounts").json()[0]["has_account"]
    ua = deps.accounts.by_username("api_a")
    ub = deps.accounts.by_username("api_b")
    api = settings.apis[0]
    tok = accounts.CURRENT.set(ua)
    try:
        assert apiauth.secret(api, "MES_USERNAME") == "kim" and not apiauth.missing(api)
    finally:
        accounts.CURRENT.reset(tok)
    tok = accounts.CURRENT.set(ub)
    try:
        assert apiauth.secret(api, "MES_USERNAME") is None and apiauth.missing(api)
        assert "내 계정" in apiauth.missing_message(api) and "다른 사람의 계정으로 대신" in apiauth.missing_message(api)
    finally:
        accounts.CURRENT.reset(tok)
    assert "kimpw" not in config.SECRETS_FILE.read_text(encoding="utf-8")


# ───────────────────────── 로컬 모델 차례 · 외부 모델

def test_local_model_runs_wait_their_turn(env, admin, monkeypatch):
    gate = threading.Event()

    def slow(messages):
        gate.wait(5)
        return reply(content="첫 작업 끝")

    script, _ = env
    a, b = issue(admin, "q_a"), issue(admin, "q_b")
    script.extend([slow, reply(content="둘째 작업 끝")])
    first = a.post("/api/v1/runs", json={"task": "느린 일", "profile": "general"}, headers=H).json()["run_id"]
    time.sleep(0.3)
    second = b.post("/api/v1/runs", json={"task": "다음 일", "profile": "general"}, headers=H)
    assert second.status_code == 200                                           # 거절(409) 대신 차례를 기다린다
    sid = second.json()["run_id"]
    deadline = time.time() + 5
    while time.time() < deadline and not any(e.get("kind") == "queue" for e in deps.runs[sid].events):
        time.sleep(0.05)
    note = next(e for e in deps.runs[sid].events if e.get("kind") == "queue")
    assert "앞에 1개" in note["message"]
    assert b.get("/api/v1/status").json()["queued"] == 1
    assert a.post("/api/v1/runs", json={"task": "또", "profile": "general"}, headers=H).status_code == 409   # 한 사람은 하나씩
    gate.set()
    assert wait_done(a, first)["status"] == "done"
    assert wait_done(b, sid)["status"] == "done"


def test_external_model_run_uses_the_connection(env, admin, monkeypatch):
    script, agents = env
    monkeypatch.setattr(providers, "_list_models", lambda p, key: [providers.ProviderModel(id="claude-opus-5-5")])
    r = admin.post("/api/v1/admin/providers", json={"kind": "anthropic", "title": "회사 Claude", "api_key": "sk-ant-xyz-9999"},
                   headers=H)
    assert r.status_code == 200 and r.json()["key_hint"] == "…9999" and "sk-ant" not in r.text
    c = issue(admin, "ext_user")
    ext = c.get("/api/v1/status").json()["external_models"]
    assert [m["key"] for m in ext] == ["ext:claude:claude-opus-5-5"] and ext[0]["ready"]
    script.append(reply(content="외부 모델 답"))
    rid = c.post("/api/v1/runs", json={"task": "안녕", "profile": "general", "model": ext[0]["key"]}, headers=H).json()["run_id"]
    run = wait_done(c, rid)
    assert agents[-1].backend.provider == "anthropic" and agents[-1].backend.model == "claude-opus-5-5"
    assert run["provider"] == "anthropic" and run["connection"] == "claude"
    script.append(reply(content="최대로 생각한 답"))   # 입력창의 추론 강도가 그 실행에만 적용된다
    rid = c.post("/api/v1/runs", json={"task": "깊게", "profile": "general", "model": ext[0]["key"], "effort": "max"},
                 headers=H).json()["run_id"]
    wait_done(c, rid)
    assert agents[-1].backend.effort == "max"
    usage = admin.get("/api/v1/admin/usage?group=connection").json()
    assert usage[0]["label"] == "회사 Claude" and usage[0]["runs"] == 2
    # 폐쇄망 스위치를 켜면 외부 모델이 사라지고 실행도 거절된다
    view = admin.get("/api/v1/admin/access").json()
    view["offline"] = True
    assert admin.put("/api/v1/admin/access", json={k: view[k] for k in ("app_name", "login_enabled", "public_hosts",
                     "password_max_age_days", "session_hours", "offline")}, headers=H).status_code == 200
    assert c.get("/api/v1/status").json()["external_models"] == []
    r = c.post("/api/v1/runs", json={"task": "다시", "profile": "general", "model": ext[0]["key"]}, headers=H)
    assert r.status_code == 400 and "폐쇄망" in r.json()["detail"]


# ───────────────────────── 접속 주소

def test_public_hosts_control_which_addresses_get_in(admin):
    other = client()
    assert other.get("/api/v1/auth/me", headers={"host": "agent.corp.example"}).status_code == 403
    view = admin.get("/api/v1/admin/access").json()
    body = {k: view[k] for k in ("app_name", "login_enabled", "public_hosts", "password_max_age_days", "session_hours", "offline")}
    body["public_hosts"] = ["http://Agent.Corp.Example:8765/"]
    saved = admin.put("/api/v1/admin/access", json=body, headers=H).json()
    assert saved["public_hosts"] == ["agent.corp.example"]
    assert other.get("/api/v1/auth/me", headers={"host": "agent.corp.example:8765"}).status_code == 200
    body["login_enabled"] = False                                              # 로그인 끄기 (혼자 쓰기로)
    assert admin.put("/api/v1/admin/access", json=body, headers=H).status_code == 200
    assert client().get("/api/v1/runs").status_code == 200


# ───────────────────────── 서비스 이름 · 추론 강도(로컬)

def test_app_name_is_shown_and_changeable(admin):
    assert client().get("/api/v1/auth/me").json()["app_name"] == "AI Agent"
    view = admin.get("/api/v1/admin/access").json()
    body = {k: view[k] for k in ("app_name", "login_enabled", "public_hosts", "password_max_age_days", "session_hours", "offline")}
    body["app_name"] = "기술지원 AI"
    assert admin.put("/api/v1/admin/access", json=body, headers=H).json()["app_name"] == "기술지원 AI"
    assert client().get("/api/v1/auth/me").json()["app_name"] == "기술지원 AI"       # 로그인 전 화면에도
    body["app_name"] = ""
    assert admin.put("/api/v1/admin/access", json=body, headers=H).status_code == 400


@pytest.mark.parametrize("effort,think", [("low", False), ("high", True), ("medium", True), ("", None)])
def test_local_effort_turns_thinking_on_or_off(env, effort, think):
    script, agents = env
    c = client()
    script.append(reply(content="답"))
    rid = c.post("/api/v1/runs", json={"task": "안녕", "profile": "general", "effort": effort}, headers=H).json()["run_id"]
    wait_done(c, rid)
    assert agents[-1].think is think and agents[-1].backend.provider == "local"


# ───────────────────────── 처음 설치: 로그인 먼저 · 첫 관리자는 서버 PC 에서만

def test_fresh_install_asks_login_first_and_first_admin_only_on_server_pc(env):
    accounts.save_access(accounts.Access())                       # 기본값 = 로그인 사용
    local = client()
    me = local.get("/api/v1/auth/me").json()
    assert me["login_enabled"] and not me["has_admin"] and me["setup_allowed"]
    assert local.get("/api/v1/runs").status_code == 401           # 로그인 전에는 아무것도 못 쓴다
    remote = TestClient(srv.app, base_url="http://127.0.0.1", client=("10.0.0.5", 50000))
    assert not remote.get("/api/v1/auth/me").json()["setup_allowed"]
    r = remote.post("/api/v1/auth/setup", json={"username": "thief", "password": "Steal1234!"}, headers=H)
    assert r.status_code == 403 and "서버 PC" in r.json()["detail"]
    r = local.post("/api/v1/auth/setup", json={"username": "boss", "password": "Adm1nPass!"}, headers=H)
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert local.get("/api/v1/runs").status_code == 200
    assert client().post("/api/v1/auth/setup", json={"username": "x", "password": "Adm1nPass!"}, headers=H).status_code == 409
    assert remote.get("/api/v1/runs").status_code == 401          # 다른 PC 는 로그인 화면부터
