"""입력창 '작업 위치': 새 작업 폴더('날짜_시각_요청')를 고른 상위 폴더 아래에 만든다 — 로컬 에이전트 (관리자만).
CLI · Claude Code 쪽 같은 규칙은 tests/test_claude_code.py 에서."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from agent import accounts, config, locations
from agent.loop import Agent
from agent.store import Store
from server import deps
from tests.conftest import FakeLLM, reply

H = {"X-Agent-Token": deps.TOKEN}


@pytest.fixture
def env(monkeypatch, settings, tmp_path):
    made: list = []
    monkeypatch.setattr(config, "CONNECTIONS_FILE", tmp_path / "connections.yaml")
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "secret" / "secrets.yaml")   # 비밀 저장소 폴더는 보호 구역 — 작업 위치와 겹치지 않게
    monkeypatch.setattr(deps, "store", Store(tmp_path / "test.db"))
    s2 = settings.model_copy(update={"session_folders": True})
    monkeypatch.setattr(deps, "load_settings", lambda: s2)
    monkeypatch.setattr(deps, "runs", {})
    monkeypatch.setattr(deps, "local_queue", deps.LocalQueue())

    def make(s, p, approver, **kw):
        made.append((s, kw))
        return Agent(s, p, llm=FakeLLM([reply(content="끝")]), approver=approver, **kw)

    monkeypatch.setattr(deps, "Agent", make)
    return made, s2


def client() -> TestClient:
    return TestClient(srv.app, base_url="http://127.0.0.1")


def wait_done(c, run_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = c.get(f"/api/v1/runs/{run_id}").json()
        if run.get("status") not in (None, "running"):
            return run
        time.sleep(0.05)
    raise AssertionError("실행이 끝나지 않음")


def test_new_task_folder_under_relative_base(env, tmp_path):
    made, s = env
    root = Path(s.workspace_path).resolve()
    c = client()
    r = c.post("/api/v1/runs", json={"task": "로그인 화면 만들어줘. 자세히", "base_dir": "projects/앱"}, headers=H)
    assert r.status_code == 200, r.text
    first = r.json()["run_id"]
    wait_done(c, first)
    run_s, kw = made[-1]
    base = root / "projects" / "앱"                                                      # 없던 폴더는 만든다
    wd = Path(kw["workdir"])
    assert wd.parent == base and wd.name.endswith("_로그인 화면 만들어줘") and wd.is_dir()
    assert Path(run_s.workspace_path) == base                                             # 이 실행의 작업 폴더 = 고른 위치
    assert any(locations.norm(run_s.path(d.path)) == locations.norm(base) and d.mode == "rw" for d in run_s.allowed_dirs)

    # 이어서 하기: 같은 폴더, 같은 작업 위치
    r = c.post("/api/v1/runs", json={"task": "버튼 색 바꿔줘", "parent_run_id": first}, headers=H)
    wait_done(c, r.json()["run_id"])
    run_s2, kw2 = made[-1]
    assert Path(kw2["workdir"]) == wd and Path(run_s2.workspace_path) == base

    # 고르지 않으면 지금처럼 workspace 바로 아래
    r = c.post("/api/v1/runs", json={"task": "그냥 작업"}, headers=H)
    wait_done(c, r.json()["run_id"])
    assert Path(made[-1][1]["workdir"]).parent == root


def test_env_root(env, tmp_path, monkeypatch):
    """환경변수 AGENT_WORK_ROOT 로 기준 폴더를 정하면 관리자의 새 작업은 그 아래 (작업 위치를 고르면 그 안의 상대 경로)."""
    made, _ = env
    monkeypatch.setenv("AGENT_WORK_ROOT", str(tmp_path / "envroot"))
    c = client()
    wait_done(c, c.post("/api/v1/runs", json={"task": "기준 폴더 작업"}, headers=H).json()["run_id"])
    assert Path(made[-1][1]["workdir"]).parent == (tmp_path / "envroot").resolve()
    wait_done(c, c.post("/api/v1/runs", json={"task": "하위 작업", "base_dir": r"a\b"}, headers=H).json()["run_id"])
    assert Path(made[-1][1]["workdir"]).parent == (tmp_path / "envroot" / "a" / "b").resolve()
    monkeypatch.setenv("AGENT_WORK_ROOT", r"C:\Windows")
    r = c.post("/api/v1/runs", json={"task": "x"}, headers=H)
    assert r.status_code == 400 and "AGENT_WORK_ROOT" in r.json()["detail"]


def test_absolute_or_escaping_base_is_refused(env, tmp_path):
    c = client()
    for bad in (str(tmp_path / "abs"), r"C:\Windows", r"\\server\share", "../밖", "a/../../밖", "a:b"):
        r = c.post("/api/v1/runs", json={"task": "x", "base_dir": bad}, headers=H)
        assert r.status_code == 400, bad


def test_base_is_admin_only(env, tmp_path):
    boss = client()
    assert boss.post("/api/v1/auth/setup", json={"username": "boss", "password": "Adm1nPass!"}, headers=H).status_code == 200
    temp = boss.post("/api/v1/admin/users", json={"username": "kim"}, headers=H).json()["temp_password"]
    kim = client()
    kim.post("/api/v1/auth/login", json={"username": "kim", "password": temp}, headers=H)
    kim.post("/api/v1/auth/password", json={"current_password": temp, "new_password": "KimPass!123"}, headers=H)
    assert kim.post("/api/v1/runs", json={"task": "x", "base_dir": "a"}, headers=H).status_code == 403
    assert kim.get("/api/v1/admin/fs/dirs").status_code == 403
    assert accounts.load_access().login_enabled


def test_folder_browser_relative(env):
    _, s = env
    root = Path(s.workspace_path)
    (root / "proj" / "child").mkdir(parents=True)
    c = client()
    top = c.get("/api/v1/admin/fs/dirs").json()
    assert top["path"] == "" and top["parent"] is None and top["root"] == str(root.resolve()) and not top["root_from_env"]
    assert "proj" in [d["path"] for d in top["dirs"]]
    r = c.get("/api/v1/admin/fs/dirs", params={"path": "proj"}).json()
    assert [d["path"] for d in r["dirs"]] == ["proj/child"] and r["parent"] == ""
    assert c.get("/api/v1/admin/fs/dirs", params={"path": "없음"}).status_code == 404
    assert c.get("/api/v1/admin/fs/dirs", params={"path": "C:\\"}).status_code == 400
    assert c.get("/api/v1/admin/fs/dirs", params={"path": ".."}).status_code == 400
