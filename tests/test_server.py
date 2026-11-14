import time

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from server import deps
from agent.loop import Agent
from agent.store import Store
from tests.conftest import FakeLLM, call, reply


@pytest.fixture
def client(monkeypatch, settings, tmp_path):
    script: list = []
    monkeypatch.setattr(deps, "store", Store(tmp_path / "test.db"))
    monkeypatch.setattr(deps, "load_settings", lambda: settings)
    monkeypatch.setattr(deps, "runs", {})
    monkeypatch.setattr(deps, "Agent", lambda s, p, approver, **kw: Agent(s, p, llm=FakeLLM(script), approver=approver, **kw))
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    c.script = script
    return c


def post(c, path, json=None, token=True):
    return c.post(path, json=json, headers={"X-Agent-Token": deps.TOKEN} if token else {})


def wait_done(c, run_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = c.get(f"/api/v1/runs/{run_id}").json()
        if run.get("status") not in (None, "running"):
            return run
        time.sleep(0.05)
    raise AssertionError("실행이 끝나지 않음")


def test_post_without_token_is_rejected(client):
    assert post(client, "/api/v1/runs", {"task": "x"}, token=False).status_code == 403


def test_foreign_host_is_rejected(client):
    assert client.get("/api/v1/runs", headers={"host": "evil.example.com"}).status_code == 403


def test_index_has_token_and_spa_routes(client, tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text('<meta name="agent-token" content="__AGENT_TOKEN__"><div id="root"></div>', encoding="utf-8")
    (dist / "logo.svg").write_text("<svg/>", encoding="utf-8")
    monkeypatch.setattr(srv, "WEB_DIST", dist)
    assert deps.TOKEN in client.get("/").text
    assert deps.TOKEN in client.get("/settings/p/coder").text           # 화면 주소는 React 가 처리 → index.html
    assert client.get("/logo.svg").text == "<svg/>"
    assert client.get("/api/v1/nope").status_code == 404                # 없는 API 는 index.html 이 아니라 404
    assert client.get("/../config/secrets.yaml").status_code in (200, 404) and "secret" not in client.get("/..%2Fconfig").text
    monkeypatch.setattr(srv, "WEB_DIST", tmp_path / "none")
    assert client.get("/").status_code == 503 and "npm run build" in client.get("/").text


def test_validation_errors_are_400_with_text(client):
    r = post(client, "/api/v1/runs", {"task": "x", "max_steps": -1})
    assert r.status_code == 400 and isinstance(r.json()["detail"], str) and "max_steps" in r.json()["detail"]


def test_run_lifecycle_and_history(client, ws):
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    client.script += [reply(call("read_file", path="a.txt")), reply(content="hello 라고 적혀 있습니다")]
    run_id = post(client, "/api/v1/runs", {"task": "a.txt 읽어줘", "profile": "general"}).json()["run_id"]
    run = wait_done(client, run_id)
    assert run["status"] == "done" and "hello" in run["answer"]
    assert [e["type"] for e in run["events"]] == ["RunStarted", "LLMCall", "ToolCall", "ToolResult", "LLMCall", "Final"]
    assert client.get("/api/v1/runs").json()[0]["id"] == run_id
    # 끝난 실행의 stream 은 DB 에서 재생된다
    body = client.get(f"/api/v1/runs/{run_id}/events").text
    assert body.count("data: {\"seq\"") == 6 and "event: end" in body


def test_approval_flow(client, ws):
    client.script += [reply(call("write_file", path="out.md", content="# 결과")), reply(content="저장했습니다")]
    run_id = post(client, "/api/v1/runs", {"task": "저장해줘", "profile": "general"}).json()["run_id"]
    handle = deps.runs[run_id]
    deadline = time.time() + 5
    while not handle.pending and time.time() < deadline:
        time.sleep(0.05)
    call_id = next(iter(handle.pending))
    assert post(client, "/api/v1/runs/{}/approvals".format(run_id), {"call_id": call_id, "approved": True}).status_code == 200
    assert wait_done(client, run_id)["status"] == "done"
    assert (ws / "out.md").read_text(encoding="utf-8") == "# 결과"


def test_cancel_while_waiting_for_approval(client, ws):
    client.script += [reply(call("write_file", path="x.md", content="x"))]
    run_id = post(client, "/api/v1/runs", {"task": "저장", "profile": "general"}).json()["run_id"]
    handle = deps.runs[run_id]
    deadline = time.time() + 5
    while not handle.pending and time.time() < deadline:
        time.sleep(0.05)
    assert post(client, f"/api/v1/runs/{run_id}/cancel").status_code == 200
    assert wait_done(client, run_id)["status"] == "cancelled"
    assert not (ws / "x.md").exists()


def test_only_one_active_run(client):
    client.script += [reply(call("write_file", path="x.md", content="x"))]
    first = post(client, "/api/v1/runs", {"task": "1", "profile": "general"}).json()["run_id"]
    assert post(client, "/api/v1/runs", {"task": "2", "profile": "general"}).status_code == 409
    post(client, f"/api/v1/runs/{first}/cancel")
    wait_done(client, first)


def test_interrupted_runs_are_marked(tmp_path):
    from agent.events import RunStarted
    st = Store(tmp_path / "x.db")
    st.record("r1", 0, RunStarted("r1", "t", "general", "m"))
    assert st.mark_interrupted() == 1
    assert st.get_run("r1")["status"] == "interrupted"
