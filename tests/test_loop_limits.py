"""스텝 한도·무제한, 반복 감지, 이어서 하기, 컨텍스트 줄이기, 자동 승인 테스트."""

import time

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

import server.app as srv
from server import deps
from agent.context import SHRUNK_NOTE, compact_messages
from agent.events import ApprovalRequested, Final, ToolResult
from agent.loop import Agent, history_from_events
from agent.store import Store
from tests.conftest import FakeLLM, call, reply


def run(agent, task="작업", **kw):
    return list(agent.run(task, **kw))


def results(events):
    return [e for e in events if isinstance(e, ToolResult)]


# ───────────────────────── 반복 감지

def test_same_write_twice_warns_and_third_time_stops(settings, profile):
    profile.auto_approve_writes = True
    same = lambda: call("write_file", path="v.py", content="from PyQt5 import Qt")   # noqa: E731
    llm = FakeLLM([reply(same()), reply(same()), reply(same()), reply(content="막힌 지점 정리")])
    events = run(Agent(settings, profile, llm=llm))
    r = results(events)
    assert "반복 경고" not in r[0].output and "반복 경고" in r[1].output
    assert not r[2].ok and "반복 감지" in r[2].output
    final = events[-1]
    assert final.status == "loop" and final.answer == "막힌 지점 정리"
    assert "같은 시도를 되풀이" in llm.calls[-1]["messages"][-1]["content"]


def test_ab_oscillation_is_caught(settings, profile):
    """실제로 있었던 일: 두 가지 틀린 수정을 번갈아 반복."""
    profile.auto_approve_writes = True
    a = lambda: reply(call("write_file", path="v.py", content="A"))   # noqa: E731
    b = lambda: reply(call("write_file", path="v.py", content="B"))   # noqa: E731
    llm = FakeLLM([a(), b(), a(), b(), a(), reply(content="정리")])
    final = run(Agent(settings, profile, llm=llm))[-1]
    assert final.status == "loop" and final.steps == 5


def test_rerunning_same_command_after_edit_is_not_a_repeat(settings, profile):
    profile.auto_approve_writes = True
    test = lambda: call("run_python", code="print(1)")   # noqa: E731
    llm = FakeLLM([reply(test()), reply(call("write_file", path="a.py", content="1")), reply(test()),
                   reply(call("write_file", path="a.py", content="2")), reply(test()), reply(content="끝")])
    events = run(Agent(settings, profile, llm=llm, approver=lambda r: True))
    assert events[-1].status == "done" and not any("반복" in r.output for r in results(events))


def test_same_read_without_changes_warns(settings, profile):
    llm = FakeLLM([reply(call("list_dir", path=".")), reply(call("list_dir", path=".")), reply(content="끝")])
    assert "반복 경고" in results(run(Agent(settings, profile, llm=llm)))[1].output


# ───────────────────────── 한도

def test_unlimited_steps_go_past_settings_limit(settings, profile):
    settings.limits.max_steps = 3
    llm = FakeLLM([reply(call("list_dir", path=f"d{i}")) for i in range(6)] + [reply(content="다 했음")])
    for i in range(6):
        (settings.workspace_path / f"d{i}").mkdir()
    final = run(Agent(settings, profile, llm=llm, max_steps=0, max_seconds=0))[-1]
    assert final.status == "done" and final.steps == 7


def test_per_run_step_limit_overrides_settings(settings, profile):
    llm = FakeLLM([reply(call("list_dir", path=".")), reply(call("search_files")), reply(content="정리")])
    final = run(Agent(settings, profile, llm=llm, max_steps=2))[-1]
    assert final.status == "max_steps" and final.steps == 2


# ───────────────────────── 이어서 하기

def test_history_rebuilds_conversation_and_fills_missing_results(settings, profile):
    llm = FakeLLM([reply(call("list_dir", path="."), content="보자"), reply(content="첫 답")])
    events = [e.to_dict() for e in run(Agent(settings, profile, llm=llm), "폴더 봐줘")]
    hist = history_from_events(events)
    assert [m["role"] for m in hist] == ["user", "assistant", "tool", "assistant"]
    assert hist[0]["content"] == "폴더 봐줘" and hist[-1]["content"] == "첫 답"
    # 결과가 없는 도구 호출(중지된 경우)은 빈 결과로 채워진다
    cut = [e for e in events if e["type"] != "ToolResult"]
    hist2 = history_from_events(cut)
    assert hist2[2] == {"role": "tool", "tool_call_id": hist[1]["tool_calls"][0]["id"], "content": "(실행되지 않음)"}


def test_continue_run_sees_previous_conversation(settings, profile):
    first = [e.to_dict() for e in run(Agent(settings, profile, llm=FakeLLM([reply(content="1차 답")])), "처음 지시")]
    llm = FakeLLM([reply(content="이어서 한 답")])
    events = run(Agent(settings, profile, llm=llm), "계속해", history=history_from_events(first), parent_run_id="r1")
    sent = llm.calls[0]["messages"]
    assert [m["content"] for m in sent[1:]] == ["처음 지시", "1차 답", "계속해"]
    assert events[0].parent_run_id == "r1"


# ───────────────────────── 컨텍스트

def test_compact_shrinks_old_tool_results_only():
    msgs = [{"role": "system", "content": "s" * 1000}] + [{"role": "tool", "tool_call_id": str(i), "content": "x" * 2000}
                                                          for i in range(10)]
    assert compact_messages(msgs, budget=5000, keep_last=2)
    assert msgs[0]["content"] == "s" * 1000                       # 시스템 프롬프트는 그대로
    assert msgs[1]["content"].endswith(SHRUNK_NOTE) and msgs[-1]["content"] == "x" * 2000
    assert not compact_messages(msgs, budget=10**9)


def test_context_overflow_error_is_retried_after_compaction(settings, profile):
    err = openai.BadRequestError("the request exceeds the available context size", body=None,
                                 response=httpx.Response(400, request=httpx.Request("POST", "http://x")))
    big = "y" * 3000
    script = [reply(call("read_file", path="big.txt")), reply(call("read_file", path="big.txt", start_line=2))]
    (settings.workspace_path / "big.txt").write_text("\n".join([big] * 5), encoding="utf-8")
    state = {"failed": False}

    def overflow_once(messages):
        if not state["failed"]:
            state["failed"] = True
            raise err
        return reply(content="줄여서 성공")

    llm = FakeLLM(script + [overflow_once, overflow_once])
    final = run(Agent(settings, profile, llm=llm))[-1]
    assert final.status == "done" and final.answer == "줄여서 성공"


# ───────────────────────── 자동 승인 (관리 화면)

@pytest.fixture
def client(monkeypatch, settings, tmp_path):
    script: list = []
    monkeypatch.setattr(deps, "store", Store(tmp_path / "t.db"))
    monkeypatch.setattr(deps, "load_settings", lambda: settings)
    monkeypatch.setattr(deps, "runs", {})
    monkeypatch.setattr(deps, "Agent", lambda s, p, approver, **kw: Agent(s, p, llm=FakeLLM(script), approver=approver, **kw))
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    c.script = script
    return c


def post(c, path, body=None):
    return c.post(path, json=body, headers={"X-Agent-Token": deps.TOKEN})


def wait(c, run_id):
    for _ in range(200):
        run = c.get(f"/api/v1/runs/{run_id}").json()
        if run.get("status") not in (None, "running"):
            return run
        time.sleep(0.05)
    raise AssertionError("안 끝남")


def test_auto_all_runs_without_asking(client, ws):
    client.script += [reply(call("write_file", path="a.txt", content="1"), call("run_python", code="print(7)")),
                      reply(content="끝")]
    run_id = post(client, "/api/v1/runs", {"task": "x", "profile": "coder", "auto_approve": "all"}).json()["run_id"]
    run = wait(client, run_id)
    assert run["status"] == "done" and (ws / "a.txt").exists()
    resolved = [e for e in run["events"] if e["type"] == "ApprovalResolved"]
    assert len(resolved) == 2 and all(e["approved"] for e in resolved)


def test_auto_writes_still_asks_for_exec_and_switch_to_all_mid_run(client, ws):
    client.script += [reply(call("write_file", path="a.txt", content="1"), call("run_python", code="print(7)")),
                      reply(content="끝")]
    run_id = post(client, "/api/v1/runs", {"task": "x", "profile": "coder", "auto_approve": "writes"}).json()["run_id"]
    handle = deps.runs[run_id]
    for _ in range(100):
        if handle.pending:
            break
        time.sleep(0.05)
    assert (ws / "a.txt").exists() and len(handle.pending) == 1       # 쓰기는 통과, 실행에서 대기
    assert client.put(f"/api/v1/runs/{run_id}/auto-approve", json={"mode": "all"}, headers={"X-Agent-Token": deps.TOKEN}).status_code == 200
    assert wait(client, run_id)["status"] == "done"


def test_continue_via_api_and_unlimited_steps(client):
    client.script += [reply(content="1차")]
    first = post(client, "/api/v1/runs", {"task": "처음", "profile": "general"}).json()["run_id"]
    wait(client, first)
    client.script[:] = [reply(content="2차")]   # FakeLLM 은 만들 때 스크립트를 복사하므로 새로 채운다
    second = post(client, "/api/v1/runs", {"task": "이어서", "profile": "general", "parent_run_id": first,
                                        "max_steps": 0}).json()["run_id"]
    run = wait(client, second)
    assert run["answer"] == "2차" and run["parent_id"] == first
    assert deps.runs[second].agent.max_steps == 0 and deps.runs[second].agent.max_seconds == 0
    assert post(client, "/api/v1/runs", {"task": "x", "parent_run_id": "없는id"}).status_code == 404
