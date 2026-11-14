"""관리 › Claude Code 탭 (server/api/claude_code.py + agent/claude_code/) — CLI 를 띄우지 않는 테스트.

데이터 폴더는 AGENT_CLAUDE_CODE_HOME 으로 임시 폴더를 쓴다 (진짜 data/claude_code 는 건드리지 않는다).
(실제 CLI 를 띄우는 시나리오 시험은 옮기기 전 별도 앱에서 했다 — docs/CLI_ClaudeCode_완료보고.md)
"""
from __future__ import annotations

import base64
import json
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

pytest.importorskip("claude_agent_sdk")

from agent import accounts  # noqa: E402
from agent.claude_code import service  # noqa: E402
from agent.claude_code.errors import AppError  # noqa: E402
from server.api import claude_code as api  # noqa: E402

B = "/api/v1/claude-code"


@pytest.fixture
def cc(tmp_path, monkeypatch):
    """Claude Code 라우터만 올린 작은 앱 (관리 서버 전체의 수명 처리 — 모델 서버 자동 시작 등 — 없이)."""
    monkeypatch.setenv("AGENT_CLAUDE_CODE_HOME", str(tmp_path / "cc"))
    monkeypatch.setattr(service, "_service", None)

    @asynccontextmanager
    async def lifespan(_):
        yield
        await service.shutdown()

    app = FastAPI(lifespan=lifespan)
    app.include_router(api.router, prefix="/api/v1")

    @app.exception_handler(AppError)
    async def _err(_: Request, e: AppError):
        return JSONResponse({"detail": e.message}, status_code=e.status_code,
                            headers={"X-Error-Code": e.code, "X-Error-Extra": json.dumps(e.extra)})

    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.put(f"{B}/settings", json={"prewarm_process": False})   # 테스트에서 CLI 를 띄우지 않는다
        yield c


def project(c, path) -> str:
    path.mkdir(exist_ok=True)
    r = c.post(f"{B}/projects", json={"path": str(path)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_projects_and_sessions(cc, tmp_path):
    pid = project(cc, tmp_path / "p")
    r = cc.post(f"{B}/projects", json={"path": str(tmp_path / "p")})
    assert r.status_code == 409 and r.headers["x-error-code"] == "project_exists"
    assert json.loads(r.headers["x-error-extra"])["project_id"] == pid and "이미" in r.json()["detail"]
    assert cc.post(f"{B}/projects", json={"path": str(tmp_path / "없음")}).status_code == 400

    s = cc.post(f"{B}/projects/{pid}/sessions", json={"title": "첫 세션", "permission_mode": "plan"}).json()
    assert s["status"] == "IDLE" and s["permission_mode"] == "plan" and uuid.UUID(s["claude_session_id"])
    assert [x["id"] for x in cc.get(f"{B}/projects/{pid}/sessions?include_external=false").json()["sessions"]] == [s["id"]]
    assert cc.get(f"{B}/sessions/{s['id']}/messages").json()["total"] == 0
    assert cc.patch(f"{B}/sessions/{s['id']}", json={"title": "바꿈"}).json()["title"] == "바꿈"
    assert cc.patch(f"{B}/sessions/{s['id']}", json={"permission_mode": "bypassPermissions"}).status_code == 422
    assert cc.get(f"{B}/sessions/last").json()["id"] == s["id"]


def test_path_change_keeps_old_session_folder(cc, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    b.mkdir()
    pid = project(cc, a)
    old = cc.post(f"{B}/projects/{pid}/sessions", json={}).json()
    r = cc.patch(f"{B}/projects/{pid}", json={"path": str(b)}).json()
    assert r["sessions_on_old_path"] == 1
    assert cc.get(f"{B}/sessions/{old['id']}").json()["working_directory"] == str(a)
    assert cc.post(f"{B}/projects/{pid}/sessions", json={}).json()["working_directory"] == str(b)


def test_missing_folder_and_offline_switch(cc, tmp_path):
    d = tmp_path / "gone"
    pid = project(cc, d)
    sid = cc.post(f"{B}/projects/{pid}/sessions", json={}).json()["id"]
    accounts.save_access(accounts.Access(login_enabled=False, offline=True))
    r = cc.post(f"{B}/sessions/{sid}/messages", json={"text": "hi"})
    assert r.status_code == 409 and r.headers["x-error-code"] == "offline"
    assert cc.get(f"{B}/status").json()["offline"] is True
    accounts.save_access(accounts.Access(login_enabled=False, offline=False))
    d.rmdir()
    r = cc.post(f"{B}/sessions/{sid}/messages", json={"text": "hi"})
    assert r.status_code == 409 and r.headers["x-error-code"] == "workdir_missing"
    assert cc.get(f"{B}/projects").json()[0]["path_exists"] is False


def test_import_unknown_and_attachments_and_delete(cc, tmp_path):
    pid = project(cc, tmp_path / "p")
    assert cc.post(f"{B}/projects/{pid}/sessions/import", json={"claude_session_id": str(uuid.uuid4())}).status_code == 404
    sid = cc.post(f"{B}/projects/{pid}/sessions", json={}).json()["id"]
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
    att = cc.post(f"{B}/sessions/{sid}/attachments", json={"name": "../a b.png", "mime": "image/png", "data_base64": png}).json()
    assert att["kind"] == "image" and ".." not in att["name"]
    assert cc.post(f"{B}/sessions/{sid}/attachments", json={"name": "x", "data_base64": "@@@"}).status_code == 400
    (tmp_path / "p" / "keep.txt").write_text("x")
    assert cc.delete(f"{B}/sessions/{sid}").status_code == 200
    assert cc.delete(f"{B}/projects/{pid}").status_code == 200
    assert (tmp_path / "p" / "keep.txt").exists()


def test_new_conversation_under_relative_base(cc, settings, monkeypatch, tmp_path):
    """작업 위치(기준 폴더 아래 상대 경로)를 고르면 그 아래에 '날짜_시각_요청' 폴더 — 로컬 에이전트와 같은 규칙."""
    from pathlib import Path

    from server import deps

    s2 = settings.model_copy(update={"session_folders": True})   # 실제 기본값 (대화마다 폴더)
    monkeypatch.setattr(deps, "load_settings", lambda: s2)
    r = cc.post(f"{B}/conversations", json={"task": "API 문서 정리해줘", "base_dir": "docs/api"})
    assert r.status_code == 201, r.text
    wd = Path(r.json()["working_directory"])
    assert wd.parent == (Path(settings.workspace_path) / "docs" / "api").resolve() and wd.name.endswith("_API 문서 정리해줘")
    for bad in (str(tmp_path), r"C:\Windows", "../밖"):
        assert cc.post(f"{B}/conversations", json={"task": "x", "base_dir": bad}).status_code == 400, bad
    monkeypatch.setenv("AGENT_WORK_ROOT", str(tmp_path / "envroot"))   # 기준 폴더를 환경변수로
    wd2 = Path(cc.post(f"{B}/conversations", json={"task": "env 기준"}).json()["working_directory"])
    assert wd2.parent == (tmp_path / "envroot").resolve()


def test_restart_recovery_marks_disconnected(tmp_path, monkeypatch):
    from agent.claude_code.cli import CliLocator
    from agent.claude_code.hub import EventHub
    from agent.claude_code.manager import SessionManager
    from agent.claude_code.settings import Settings
    from agent.claude_code.store import Database

    db = Database(tmp_path / "t.db")
    m = SessionManager(db, Settings(db), EventHub(), CliLocator(), tmp_path / "up")
    p = db.create_project("p", str(tmp_path))
    running = db.create_session(p["id"], str(uuid.uuid4()), "t", str(tmp_path), "RUNNING")
    db.add_process(running["id"], 999_999, "BUSY", None, None, 0.0)
    assert m.recover() == {"disconnected": 1, "orphans": 0}
    assert db.get_session(running["id"])["status"] == "DISCONNECTED"
    assert db.recent_processes()[0]["status"] == "LOST"


def test_message_normalization():
    from agent.claude_code.transcript import normalize_raw

    assert normalize_raw("u", "user", {"role": "user", "content": "안녕"})["kind"] == "prompt"
    tr = normalize_raw("u2", "user", {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": "x" * 40_000}]}]})
    assert tr["kind"] == "tool_results" and tr["blocks"][0]["truncated"]
    assert normalize_raw("u3", "user", {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]})["kind"] == "notice"


def test_admin_only_and_login_required():
    """관리 서버 전체 앱: 로그인 사용 모드에서 로그인 없이는 401, 일반 사용자는 403 (관리자만)."""
    import server.app as srv
    from server import deps

    H = {"X-Agent-Token": deps.TOKEN}

    def cl():
        return TestClient(srv.app, base_url="http://127.0.0.1")

    boss = cl()
    assert boss.post("/api/v1/auth/setup", json={"username": "boss", "password": "Adm1nPass!"}, headers=H).status_code == 200
    assert cl().get(f"{B}/status").status_code == 401
    temp = boss.post("/api/v1/admin/users", json={"username": "kim"}, headers=H).json()["temp_password"]
    kim = cl()
    assert kim.post("/api/v1/auth/login", json={"username": "kim", "password": temp}, headers=H).status_code == 200
    assert kim.post("/api/v1/auth/password", json={"current_password": temp, "new_password": "KimPass!123"}, headers=H).status_code == 200
    assert kim.get(f"{B}/status").status_code == 403
    assert kim.post(f"{B}/projects", json={"path": "C:\\"}, headers=H).status_code == 403


def test_new_conversation_makes_work_folder_like_agent(cc, settings, monkeypatch):
    """새 CLI 대화: 폴더를 고르지 않아도 에이전트의 새 작업처럼 workspace/날짜_시각_요청 폴더를 만들어 그 안에서 시작한다."""
    from pathlib import Path

    from server import deps

    s2 = settings.model_copy(update={"session_folders": True})
    monkeypatch.setattr(deps, "load_settings", lambda: s2)
    a = cc.post(f"{B}/conversations", json={"task": "사진 뷰어 만들어줘. (자세히)", "model": "haiku"})
    assert a.status_code == 201, a.text
    wd = Path(a.json()["working_directory"])
    assert wd.is_dir() and wd.parent == Path(s2.workspace_path).resolve() and wd.name.endswith("_사진 뷰어 만들어줘")
    b = cc.post(f"{B}/conversations", json={"task": "사진 뷰어 만들어줘"}).json()
    assert Path(b["working_directory"]) != wd                              # 대화마다 새 폴더
    projects = cc.get(f"{B}/projects").json()
    assert len(projects) == 1 and projects[0]["path"] == str(Path(s2.workspace_path).resolve())   # 목록에는 workspace 하나로 묶임
    assert a.json()["project_id"] == b["project_id"] == projects[0]["id"]


# ───────── CLI 도 로컬·API 와 같은 '설정 · 프롬프트' · 하네스를 따른다

def test_cli_instructions_use_same_prompts_and_harness(tmp_path):
    from agent import harness
    from agent.claude_code import instructions

    harness.root().mkdir(parents=True, exist_ok=True)
    (harness.root() / "AGENT.md").write_text("회사 공통 규칙: 보고는 항상 표로 한다 (XYZ-1).\n", encoding="utf-8")
    (harness.root() / "agents").mkdir(exist_ok=True)
    (harness.root() / "agents" / "검토자.md").write_text("---\nname: 검토자\ndescription: 코드 검토 담당\n---\n변경을 검토하고 문제를 표로 적는다.\n",
                                                        encoding="utf-8")
    r = instructions.build("coder", tmp_path)
    ids = [s["id"] for s in r["sections"]]
    assert r["profile"] == "coder" and "role" in ids and "harness" in ids and "dev_standard" in ids
    assert not {"environment", "intranet_status", "lang_standard"} & set(ids)        # 로컬 에이전트 전용 조각은 뺀다
    assert "XYZ-1" in r["text"] and "read_file→Read" in r["text"]
    assert [x["name"] for x in r["roles"]] == ["검토자"]
    agents = instructions.sdk_agents()
    assert agents and agents["검토자"].prompt.startswith("변경을 검토") and agents["검토자"].description == "코드 검토 담당"
    assert instructions.build("없는용도")["profile"] == "general"                       # 모르는 용도면 기본


def test_session_profile_effort_and_cli_options(cc, tmp_path, monkeypatch):
    """대화의 용도·추론 강도가 저장되고, CLI 를 띄울 때 지침(append)·서브에이전트·--effort 로 넘어간다."""
    from agent import harness
    from agent.claude_code import manager as mgr
    from agent.claude_code.cli import CliInfo

    harness.root().mkdir(parents=True, exist_ok=True)
    (harness.root() / "AGENT.md").write_text("공통 규칙 ABC-9\n", encoding="utf-8")
    pid = project(cc, tmp_path / "p")
    s = cc.post(f"{B}/projects/{pid}/sessions", json={"profile": "coder", "effort": "high"}).json()
    assert s["profile"] == "coder" and s["effort"] == "high"
    assert cc.patch(f"{B}/sessions/{s['id']}", json={"effort": "low"}).json()["effort"] == "low"
    ins = cc.get(f"{B}/instructions", params={"session_id": s["id"]}).json()
    assert ins["profile"] == "coder" and "ABC-9" in ins["text"]

    captured = {}
    svc = service.get_service()
    monkeypatch.setattr(svc.locator, "locate", lambda *a, **k: CliInfo(found=True, path="claude.exe", source="test", version="0"))

    class Stop(Exception):
        pass

    async def fake_start(session_id, options, version):
        captured["opts"] = options
        raise Stop()

    monkeypatch.setattr(svc.manager.procs, "start", fake_start)
    monkeypatch.setattr(mgr, "external_session_holders", lambda *a, **k: [])
    r = cc.post(f"{B}/sessions/{s['id']}/process/start", json={"force": False})
    assert r.status_code == 502                                                      # 가짜 시작이 멈춘 것
    o = captured["opts"]
    assert o.effort == "low" and "ABC-9" in o.system_prompt["append"] and o.system_prompt["preset"] == "claude_code"
    assert str(harness.root()) in [str(x) for x in o.add_dirs]
    from agent import config
    excludes = json.loads(o.settings)["claudeMdExcludes"]   # 이 프로그램 폴더의 개발용 CLAUDE.md 는 CLI 대화에 붙지 않게
    assert f"{config.ROOT.resolve().as_posix()}/CLAUDE.md" in excludes

    svc.db.update_session(s["id"], has_transcript=True)
    assert cc.patch(f"{B}/sessions/{s['id']}", json={"profile": "general"}).status_code == 409   # 시작한 대화의 용도는 고정


def test_slash_commands_from_cli_cached_per_engine(cc, tmp_path, monkeypatch):
    """입력창 '/' 목록: CLI 를 띄우기 전엔 기본 목록, 띄우면 그 CLI 가 준 목록(숨길 것 빼고) → 엔진별로 남겨 다음 새 대화에도."""
    import asyncio

    from agent.claude_code import manager as mgr
    from agent.claude_code.cli import CliInfo
    from agent.claude_code.procs import ProcessHandle

    first = cc.get(f"{B}/commands", params={"engine": "claude"}).json()
    assert first["source"] == "default" and "compact" in [c["name"] for c in first["commands"]]
    assert cc.get(f"{B}/commands", params={"engine": "codex"}).json() == {"engine": "codex", "source": "none", "commands": []}

    class FakeClient:
        async def get_server_info(self):
            return {"commands": [
                {"name": "compact", "description": "Free up context", "argumentHint": "<instructions>", "builtin": True},
                {"name": "my-cmd", "description": "custom (project)", "argumentHint": "", "aliases": ["mc"]},
                {"name": "heapdump", "description": "Dump the JS heap", "builtin": True},
                {"name": "agents", "description": "(removed) Ask Claude", "builtin": True},
                {"name": "__remote-workflow", "description": "internal", "builtin": True}]}

        async def receive_messages(self):
            await asyncio.Event().wait()
            yield

        async def disconnect(self):
            pass

    svc = service.get_service()
    monkeypatch.setattr(svc.locator, "locate", lambda *a, **k: CliInfo(found=True, path="claude.exe", source="test", version="0"))

    async def fake_start(session_id, options, version):
        return ProcessHandle(session_id, FakeClient(), None, None, None, "claude.exe", "0", status="READY")

    monkeypatch.setattr(svc.manager.procs, "start", fake_start)
    monkeypatch.setattr(mgr, "external_session_holders", lambda *a, **k: [])
    sid = cc.post(f"{B}/projects/{project(cc, tmp_path / 'p')}/sessions", json={}).json()["id"]
    assert cc.post(f"{B}/sessions/{sid}/process/start", json={"force": False}).status_code == 200
    got = cc.get(f"{B}/commands", params={"session_id": sid}).json()
    assert got["source"] == "session" and [c["name"] for c in got["commands"]] == ["compact", "my-cmd"]
    compact = got["commands"][0]
    assert compact["hint"] == "<instructions>" and compact["builtin"] and compact["description_en"] == "Free up context"
    assert "요약" in compact["description"] and got["commands"][1]["aliases"] == ["mc"]
    assert cc.get(f"{B}/sessions/{sid}/snapshot").json()["commands"] == got["commands"]
    cc.post(f"{B}/sessions/{sid}/process/stop")
    later = cc.get(f"{B}/commands", params={"engine": "claude"}).json()
    assert later["source"] == "cache" and later["commands"] == got["commands"]
