"""CLI 엔진 Codex · Gemini (agent/claude_code/engines.py): 가짜 CLI(tests/fake_engine_cli.py)로 진짜 CLI · 로그인 없이
같은 지침(설정 · 프롬프트 + 하네스) 붙이기 · 스트리밍 · 승인 패널 · 기록 · 재개 · 권한 모드를 API 로 시험한다."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")

from agent import harness  # noqa: E402
from agent.claude_code import engines  # noqa: E402
from tests.test_claude_code import B, cc, project  # noqa: E402,F401  (cc 는 fixture)

FAKE = Path(__file__).with_name("fake_engine_cli.py")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    logf = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_ENGINE_LOG", str(logf))
    home = tmp_path / "ghome"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")   # 로그인한 것으로
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    monkeypatch.setattr(engines, "locate", lambda engine, override="": engines.EngineCli(
        engine, True, [sys.executable, str(FAKE)], str(FAKE), "시험", "0.0"))
    harness.root().mkdir(parents=True, exist_ok=True)
    (harness.root() / "AGENT.md").write_text("공통 규칙 ABC-9\n", encoding="utf-8")

    def entries(kind: str | None = None) -> list:
        if not logf.is_file():
            return []
        rows = [json.loads(x) for x in logf.read_text(encoding="utf-8").splitlines() if x.strip()]
        return [r["data"] for r in rows if kind is None or r["kind"] == kind]
    return entries


def wait(c, sid: str, pred, timeout: float = 30):
    end, s = time.time() + timeout, None
    while time.time() < end:
        s = c.get(f"{B}/sessions/{sid}").json()
        if pred(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"기다린 상태가 오지 않음: {s}")


def history(c, sid: str) -> list[dict]:
    return c.get(f"{B}/sessions/{sid}/messages").json()["messages"]


def pending(c, sid: str) -> dict:
    return c.get(f"{B}/sessions/{sid}/snapshot").json()["pending"][0]


def test_codex_driver_instructions_approval_history_resume(cc, fake, tmp_path):  # noqa: F811
    pid = project(cc, tmp_path / "p")
    s = cc.post(f"{B}/projects/{pid}/sessions", json={"engine": "codex", "profile": "coder", "effort": "low"}).json()
    assert s["engine"] == "codex" and s["model"] is None
    sid = s["id"]
    assert cc.post(f"{B}/sessions/{sid}/messages", json={"text": "echo 해줘", "attachments": []}).status_code == 200
    wait(cc, sid, lambda x: x["status"] == "WAITING_FOR_PERMISSION")
    p = pending(cc, sid)
    assert p["tool_name"] == "Bash" and p["input"]["command"] == "echo hello"     # PowerShell 감싸기를 벗겨 보여 준다
    assert cc.post(f"{B}/sessions/{sid}/permissions/{p['id']}", json={"behavior": "allow"}).status_code == 200
    done = wait(cc, sid, lambda x: x["status"] in ("COMPLETED", "ERROR"))
    assert done["status"] == "COMPLETED" and done["claude_session_id"] == "thr-fake-1" and done["has_transcript"]
    start = fake("thread/start")[0]
    assert "ABC-9" in start["developerInstructions"] and "Codex 에서는" in start["developerInstructions"]
    assert start["approvalPolicy"] == "untrusted" and start["approvalsReviewer"] == "user" and start["config"]["model_reasoning_effort"] == "low"
    assert fake("turn/start")[0]["effort"] == "low" and fake("decision") == ["accept"]
    msgs = history(cc, sid)
    assert [m["kind"] for m in msgs] == ["prompt", "assistant", "assistant", "tool_results", "assistant"]
    assert msgs[0]["blocks"][0]["text"] == "echo 해줘" and msgs[1]["blocks"][0]["type"] == "thinking"
    assert msgs[2]["blocks"][0]["name"] == "Bash" and msgs[3]["blocks"][0]["content"].startswith("hello")
    assert msgs[4]["blocks"][0]["text"] == "다 했습니다"
    # 프로세스를 닫고 다시 보내면 같은 스레드를 이어받는다 (지침도 다시)
    assert cc.post(f"{B}/sessions/{sid}/process/stop").status_code == 200
    cc.post(f"{B}/sessions/{sid}/messages", json={"text": "다시", "attachments": []})
    p = pending(cc, wait(cc, sid, lambda x: x["status"] == "WAITING_FOR_PERMISSION")["id"])
    cc.post(f"{B}/sessions/{sid}/permissions/{p['id']}", json={"behavior": "deny"})
    wait(cc, sid, lambda x: x["status"] == "COMPLETED")
    resumed = fake("thread/resume")[0]
    assert resumed["threadId"] == "thr-fake-1" and resumed["excludeTurns"] and "ABC-9" in resumed["developerInstructions"]
    assert fake("decision")[-1] == "decline" and "거부" in history(cc, sid)[-2]["blocks"][0]["content"]
    # plan 모드: 실행·고치기는 묻지 않고 거절
    cc.patch(f"{B}/sessions/{sid}", json={"permission_mode": "plan"})
    cc.post(f"{B}/sessions/{sid}/messages", json={"text": "또", "attachments": []})
    wait(cc, sid, lambda x: x["status"] == "COMPLETED" and len(fake("decision")) == 3)
    assert fake("decision")[-1] == "decline"
    cc.post(f"{B}/sessions/{sid}/process/stop")


def test_gemini_driver_instructions_once_permission_modes_and_load(cc, fake, tmp_path):  # noqa: F811
    pid = project(cc, tmp_path / "p")
    sid = cc.post(f"{B}/projects/{pid}/sessions", json={"engine": "gemini", "profile": "coder"}).json()["id"]
    cc.post(f"{B}/sessions/{sid}/messages", json={"text": "파일 만들어", "attachments": []})
    wait(cc, sid, lambda x: x["status"] == "WAITING_FOR_PERMISSION")
    p = pending(cc, sid)
    assert p["tool_name"] == "Write" and p["input"] == {"file_path": "a.txt", "content": "hi"}
    cc.post(f"{B}/sessions/{sid}/permissions/{p['id']}", json={"behavior": "deny"})
    done = wait(cc, sid, lambda x: x["status"] in ("COMPLETED", "ERROR"))
    assert done["status"] == "COMPLETED" and done["claude_session_id"] == "gs-fake-1"
    assert fake("permission")[0]["outcome"] == {"outcome": "selected", "optionId": "cancel"}   # '항상 허용'은 고르지 않는다
    first = fake("session/prompt")[0]["prompt"][0]["text"]
    assert first.startswith("<agent-server-instructions>") and "ABC-9" in first and "google_web_search" in first
    assert first.endswith("파일 만들어")
    msgs = history(cc, sid)
    texts = [b.get("text") for m in msgs for b in m["blocks"] if b["type"] == "text"]
    assert msgs[0]["blocks"][0]["text"] == "파일 만들어"                       # 화면 기록에는 지침을 붙이지 않은 요청
    assert "파일을 만듭니다" in texts and "끝" in texts and not any("MODE_UPDATE" in (t or "") for t in texts)
    assert any(b["type"] == "tool_result" and b["is_error"] for m in msgs for b in m["blocks"])
    # acceptEdits: 파일 고치기는 묻지 않고 허용, 지침은 첫 요청에만
    cc.patch(f"{B}/sessions/{sid}", json={"permission_mode": "acceptEdits"})
    cc.post(f"{B}/sessions/{sid}/messages", json={"text": "한 번 더", "attachments": []})
    wait(cc, sid, lambda x: x["status"] == "COMPLETED" and len(fake("permission")) == 2)
    assert fake("permission")[-1]["outcome"]["optionId"] == "proceed_once"
    assert not fake("session/prompt")[1]["prompt"][0]["text"].startswith("<agent-server")
    # 다시 띄우면 session/load 로 이어받고, 다시 흘러온 옛 기록은 화면 기록에 넣지 않는다
    cc.post(f"{B}/sessions/{sid}/process/stop")
    n = len(history(cc, sid))
    assert cc.post(f"{B}/sessions/{sid}/process/start", json={"force": False}).status_code == 200
    assert fake("session/load")[0]["sessionId"] == "gs-fake-1" and len(history(cc, sid)) == n
    cc.post(f"{B}/sessions/{sid}/process/stop")


def test_engine_status_and_errors(cc, fake, tmp_path, monkeypatch):  # noqa: F811
    st = cc.get(f"{B}/status").json()
    by = {e["engine"]: e for e in st["engines"]}
    assert by["codex"]["installed"] and by["codex"]["logged_in"] and by["codex"]["account"] == "t@example.com"
    assert by["codex"]["models"][0]["id"] == "gpt-fake" and by["gemini"]["logged_in"]
    pid = project(cc, tmp_path / "p")
    assert cc.post(f"{B}/projects/{pid}/sessions", json={"engine": "nope"}).status_code == 422
    # 로그인 안 된 Gemini 는 시작하지 않고 알린다
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "empty"))
    sid = cc.post(f"{B}/projects/{pid}/sessions", json={"engine": "gemini"}).json()["id"]
    r = cc.post(f"{B}/sessions/{sid}/process/start", json={"force": False})
    assert r.status_code == 409 and "로그인" in r.json()["detail"]
    # 폐쇄망 스위치면 엔진도 막힌다
    from agent import accounts
    acc = accounts.load_access().model_copy(update={"offline": True})
    monkeypatch.setattr(accounts, "load_access", lambda: acc)
    sid2 = cc.post(f"{B}/projects/{pid}/sessions", json={"engine": "codex"}).json()["id"]
    assert cc.post(f"{B}/sessions/{sid2}/process/start", json={"force": False}).status_code == 409
    assert cc.post(f"{B}/engines/codex/login").status_code == 409


def test_codex_command_and_patch_shapes():
    assert engines._codex_command('"C:\\x\\powershell.exe" -Command \'Get-ChildItem\'') == "Get-ChildItem"
    assert engines._codex_command(["git", "status"]) == "git status"
    assert engines._codex_patch([{"path": "a.py", "kind": {"type": "add"}, "diff": "x"}]) == ("Write", {"file_path": "a.py", "content": "x"})
    name, inp = engines._codex_patch([{"path": "a.py", "kind": {"type": "update", "move_path": None}, "diff": "@@ -1 +1 @@"}])
    assert name == "Patch" and inp["changes"][0]["kind"] == "update"
    assert engines._gemini_tool({"kind": "execute", "title": "ls"})[::2] == ("Bash", "command")


def test_gemini_slash_commands_and_first_message_command(cc, fake, tmp_path):  # noqa: F811
    """Gemini 가 알려 준 '/' 명령을 목록으로, 첫 메시지가 '/' 명령이면 지침을 앞에 붙이지 않고 다음 메시지에 붙인다."""
    pid = project(cc, tmp_path / "p")
    sid = cc.post(f"{B}/projects/{pid}/sessions", json={"engine": "gemini", "profile": "coder"}).json()["id"]
    cc.post(f"{B}/sessions/{sid}/messages", json={"text": "/memory show", "attachments": []})
    p = pending(cc, wait(cc, sid, lambda x: x["status"] == "WAITING_FOR_PERMISSION")["id"])
    cc.post(f"{B}/sessions/{sid}/permissions/{p['id']}", json={"behavior": "deny"})
    wait(cc, sid, lambda x: x["status"] == "COMPLETED")
    assert fake("session/prompt")[0]["prompt"][0]["text"] == "/memory show"
    got = cc.get(f"{B}/commands", params={"session_id": sid}).json()
    assert got["engine"] == "gemini" and got["source"] == "session"
    assert [(c["name"], c["hint"]) for c in got["commands"]] == [("memory", ""), ("restore", "<checkpoint>")]
    cc.post(f"{B}/sessions/{sid}/messages", json={"text": "파일 만들어", "attachments": []})
    p = pending(cc, wait(cc, sid, lambda x: x["status"] == "WAITING_FOR_PERMISSION")["id"])
    cc.post(f"{B}/sessions/{sid}/permissions/{p['id']}", json={"behavior": "deny"})
    wait(cc, sid, lambda x: x["status"] == "COMPLETED" and len(fake("session/prompt")) == 2)
    second = fake("session/prompt")[1]["prompt"][0]["text"]
    assert second.startswith("<agent-server-instructions>") and "ABC-9" in second and second.endswith("파일 만들어")
    cc.post(f"{B}/sessions/{sid}/process/stop")
    assert cc.get(f"{B}/commands", params={"engine": "gemini"}).json()["source"] == "cache"
