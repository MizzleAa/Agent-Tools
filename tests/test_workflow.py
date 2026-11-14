"""워크플로: 노드 순서·결과 넘기기·실패 처리·이어서 하기·서버 실행·설정 API."""

import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import server.app as srv
from server import deps
from agent import workflow as wfmod
from agent.events import Final, LLMCall, NodeFinished, NodeStarted, RunStarted, WorkflowStarted
from agent.loop import history_from_events
from agent.store import Store
from agent.workflow import Workflow, WorkflowRunner, resume_state
from tests.conftest import FakeLLM, reply

PLAN = "파일을 확인하고 정리해서 작성하겠습니다."   # 도구 없이 예고만 → 되돌려 보내기 2번 뒤 no_action


def wf(*nodes, name="t"):
    return Workflow.model_validate({"name": name, "nodes": list(nodes)})


def node(id, prompt="{{input}}", **kw):
    return {"id": id, "profile": "general", "prompt": prompt, **kw}


def run(settings, workflow, script, **kw):
    llm = FakeLLM(script)
    events = list(WorkflowRunner(settings, workflow, llm=llm, **kw).run("주제 X"))
    return events, llm


def test_validation():
    w = wf(node("a"), node("b"), node("c", after=["a"]))
    assert [w.deps(n) for n in w.nodes] == [[], ["a"], ["a"]]
    for bad in ([node("a"), node("a")], [node("a", after=["b"]), node("b")], [node("input")], [node("Bad")], []):
        with pytest.raises(ValidationError):
            Workflow.model_validate({"name": "t", "nodes": bad})
    with pytest.raises(FileNotFoundError):
        WorkflowRunner(None, wf({"id": "a", "profile": "nope", "prompt": "x"}))


def test_nodes_run_in_order_with_fresh_context_and_pass_results(settings, ws):
    w = wf(node("research", "조사: {{input}}"), node("write", "보고서로: {{research}} / {{없는변수}}"))
    events, llm = run(settings, w, [reply(content="사실 1, 사실 2"), reply(content="보고서 완성")], workdir=ws)
    types = [type(e).__name__ for e in events]
    assert types[:3] == ["RunStarted", "WorkflowStarted", "NodeStarted"] and types[-1] == "Final"
    assert types.count("RunStarted") == 1 and types.count("Final") == 1       # 노드의 시작·끝은 노드 이벤트로 바뀐다
    assert llm.calls[0]["messages"][-1]["content"] == "조사: 주제 X"
    second = llm.calls[1]["messages"]
    assert len(second) == 2 and second[-1]["content"] == "보고서로: 사실 1, 사실 2 / {{없는변수}}"   # 새 컨텍스트
    final = events[-1]
    assert final.status == "done" and final.answer == "보고서 완성"
    assert (ws / "_workflow" / "01_research.md").read_text(encoding="utf-8").endswith("사실 1, 사실 2\n")
    log = (ws / "작업기록.md").read_text(encoding="utf-8")
    assert log.count("## 1.") == 1 and "▶ 노드 1/2 research (general)" in log and "write → 완료" in log
    assert history_from_events([e.to_dict() for e in events]) == [
        {"role": "user", "content": "주제 X"}, {"role": "assistant", "content": "보고서 완성"}]


def test_failed_node_stops_or_skips_dependents(settings):
    events, _ = run(settings, wf(node("a"), node("b")), [reply(content=PLAN)] * 3)
    final = events[-1]
    assert final.status == "no_action" and "'a' 노드에서 멈췄습니다" in final.answer
    assert not [e for e in events if isinstance(e, NodeStarted) and e.node == "b"]

    w = wf(node("a", on_fail="continue"), node("b", after=["a"]), node("c", after=[]))
    events, _ = run(settings, w, [reply(content=PLAN)] * 3 + [reply(content="c 결과")])
    done = {e.node: e.status for e in events if isinstance(e, NodeFinished)}
    assert done == {"a": "no_action", "b": "skipped", "c": "done"}
    assert events[-1].status == "partial" and "끝나지 못한 노드: a, b" in events[-1].answer


def test_long_output_is_clipped_for_next_node(settings, ws):
    long = "가" * (wfmod.OUTPUT_CHARS + 3000)
    _, llm = run(settings, wf(node("a"), node("b", "{{a}}")), [reply(content=long), reply(content="끝")], workdir=ws)
    sent = llm.calls[1]["messages"][-1]["content"]
    assert len(sent) < wfmod.OUTPUT_CHARS + 200 and "_workflow/01_a.md" in sent


def test_resume_reuses_finished_nodes(settings, ws):
    w = wf(node("a"), node("b", "{{a}} 로 마무리"))
    first, _ = run(settings, w, [reply(content="A 결과")] + [reply(content=PLAN)] * 3, workdir=ws)
    assert first[-1].status == "no_action"
    done, original, note = resume_state([e.to_dict() for e in first], w)
    assert done == {"a": "A 결과"} and original == "주제 X" and note
    llm = FakeLLM([reply(content="B 완료")])
    events = list(WorkflowRunner(settings, w, llm=llm, resume=done, resume_note=note, input_text=original,
                                 workdir=ws).run("멈춘 노드부터 계속"))
    assert [(e.node, e.status) for e in events if isinstance(e, NodeFinished)] == [("a", "reused"), ("b", "done")]
    prompt = llm.calls[0]["messages"][-1]["content"]
    assert prompt.startswith("A 결과 로 마무리") and "이전 시도가 끝나지 않고 멈췄습니다" in prompt
    assert events[0].task == "멈춘 노드부터 계속" and events[1].input == "주제 X" and events[-1].status == "done"


def test_cancel_between_nodes(settings):
    runner = WorkflowRunner(settings, wf(node("a"), node("b")), llm=FakeLLM([reply(content="A")]))
    out = []
    for ev in runner.run("x"):
        out.append(ev)
        if isinstance(ev, NodeFinished):
            runner.cancel.set()
    assert out[-1].status == "cancelled" and sum(isinstance(e, NodeStarted) for e in out) == 1


# ───────────────────────── 서버

@pytest.fixture
def client(monkeypatch, settings, tmp_path):
    settings.session_folders = True
    script: list = []
    folder = tmp_path / "workflows"
    folder.mkdir()
    (folder / "two.yaml").write_text(wf(node("a", "{{input}}"), node("b", "B: {{a}}"), name="two").to_yaml(), encoding="utf-8")
    monkeypatch.setattr(wfmod, "WORKFLOWS_DIR", folder)
    monkeypatch.setattr(deps, "store", Store(tmp_path / "t.db"))
    monkeypatch.setattr(deps, "load_settings", lambda: settings)
    monkeypatch.setattr(deps, "runs", {})
    monkeypatch.setattr(deps, "WorkflowRunner", lambda s, w, **kw: WorkflowRunner(s, w, llm=FakeLLM(script), **kw))
    c = TestClient(srv.app, base_url="http://127.0.0.1", headers={"X-Agent-Token": deps.TOKEN})
    c.script = script
    return c


def wait(c, run_id):
    for _ in range(200):
        r = c.get(f"/api/v1/runs/{run_id}").json()
        if r.get("status") not in (None, "running"):
            return r
        time.sleep(0.05)
    raise AssertionError("안 끝남")


def test_server_runs_workflow_and_resumes(client):
    assert [w["name"] for w in client.get("/api/v1/workflows").json()] == ["two"]
    client.script[:] = [reply(content="A 결과")] + [reply(content=PLAN)] * 3
    first = client.post("/api/v1/runs", json={"task": "일", "workflow": "two"}).json()["run_id"]
    r = wait(client, first)
    assert r["status"] == "no_action" and r["profile"] == "workflow:two"
    assert [e["type"] for e in r["events"]].count("NodeFinished") == 2
    client.script[:] = [reply(content="B 완료")]
    second = client.post("/api/v1/runs", json={"task": "멈춘 노드부터 계속", "workflow": "two", "resume": True,
                                            "parent_run_id": first}).json()["run_id"]
    r2 = wait(client, second)
    assert r2["status"] == "done" and r2["answer"] == "B 완료" and r2["workdir"] == r["workdir"]
    assert client.post("/api/v1/runs", json={"task": "x", "workflow": "nope"}).status_code == 404


def test_settings_api_workflows(client, tmp_path, monkeypatch):
    from agent import config
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)   # 변경 기록이 tmp_path/workflows 를 config 안으로 보게
    S = "/api/v1/settings/workflows"
    d = client.get("/api/v1/settings").json()
    assert [w["name"] for w in d["workflows"]] == ["two"]
    assert d["workflows"][0]["workflow"]["nodes"][1]["after"] == ["a"]   # 비어 있던 '바로 앞 노드'를 채워 보낸다
    new = {"name": "chain", "description": "", "nodes": [node("a"), node("b", "{{a}} {{zzz}} {{c}}")]}
    r = client.post(S, json=new)
    assert r.status_code == 201
    warns = " / ".join(r.json()["warnings"])
    assert "{{zzz}}" in warns and "{{c}}" in warns
    for bad in ({**new, "nodes": [node("a", after=["z"])]}, {**new, "nodes": [{"id": "a", "profile": "nope", "prompt": "x"}]},
                {**new, "nodes": []}):
        r = client.put(f"{S}/chain", json=bad)
        assert r.status_code == 400 and isinstance(r.json()["detail"], str), bad
    assert client.post(S, json={**new, "name": "Bad Name"}).status_code == 400
    assert client.post(S, json=new).status_code == 409
    assert client.put(f"{S}/other", json=new).status_code == 400
    assert client.delete(f"{S}/chain").status_code == 200
    assert not (tmp_path / "workflows" / "chain.yaml").exists()


def test_default_workflows_are_valid():
    for name in wfmod.list_workflows(wfmod.DEFAULT_DIR):
        w = wfmod.load_workflow(name, wfmod.DEFAULT_DIR)
        assert w.to_yaml() == (wfmod.DEFAULT_DIR / f"{name}.yaml").read_text(encoding="utf-8")
        WorkflowRunner(None, w)   # 노드의 프로필이 모두 있다
