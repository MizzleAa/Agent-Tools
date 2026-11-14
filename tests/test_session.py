"""대화(세션)별 작업 폴더와 작업기록.md / 작업목록.md 테스트."""

import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from server import deps
from server.api import runs as runs_api
from agent import session
from agent.loop import Agent, history_from_events
from agent.safety import SandboxError
from agent.store import Store
from tests.conftest import FakeLLM, call, reply


def test_folder_name_is_safe_and_dated():
    name = session.folder_name('사진 뷰어 <만들어>: "C:\\x"?*\n두 번째 줄', datetime(2026, 11, 5, 14, 30))
    assert name.startswith("2026-11-05_1430_사진 뷰어 만들어")
    assert not any(c in name for c in '<>:"/\\|?*') or name[4] == "-"
    assert "두 번째" not in name


def test_create_session_dir_is_unique(ws):
    a = session.create_session_dir(ws, "같은 작업")
    b = session.create_session_dir(ws, "같은 작업")
    assert a != b and a.is_dir() and b.name.endswith("-2")


def test_agent_works_inside_session_folder_and_writes_log(settings, profile, ws):
    profile.auto_approve_writes = True
    folder = session.create_session_dir(ws, "뷰어 만들어줘")
    llm = FakeLLM([reply(call("write_file", path="viewer.py", content="print('hi')")),
                   reply(call("run_python", code="open('out.txt','w').write('ok')")),
                   reply(content="viewer.py 를 만들었습니다.")])
    events = list(Agent(settings, profile, llm=llm, approver=lambda r: True, workdir=folder).run("뷰어 만들어줘"))
    assert (folder / "viewer.py").exists() and (folder / "out.txt").exists()   # 상대 경로·실행 위치 = 세션 폴더
    assert not (ws / "viewer.py").exists()
    assert events[0].workdir == str(folder)
    log = (folder / session.LOG_NAME).read_text(encoding="utf-8")
    assert log.startswith("# 뷰어 만들어줘") and "## 1. 뷰어 만들어줘" in log
    assert "- 파일 저장: `viewer.py`" in log and "파이썬 실행" in log and "(종료 코드 0)" in log
    assert "viewer.py 를 만들었습니다." in log and "- `viewer.py`" in log and "결과: 완료" in log

    # 이어서 하기 → 같은 폴더, 기록에 2번 절이 붙는다
    llm2 = FakeLLM([reply(content="확대 기능도 넣었습니다.")])
    list(Agent(settings, profile, llm=llm2, workdir=folder).run("확대 기능 추가", history=history_from_events(
        [e.to_dict() for e in events])))
    log = (folder / session.LOG_NAME).read_text(encoding="utf-8")
    assert "## 2. 확대 기능 추가" in log and log.count("# 뷰어 만들어줘") == 1
    index = (ws / session.INDEX_NAME).read_text(encoding="utf-8")
    assert folder.name in index and "| 2 | 완료 |" in index


def test_denied_and_failed_steps_are_recorded(settings, profile, ws):
    folder = session.create_session_dir(ws, "저장")
    llm = FakeLLM([reply(call("write_file", path="a.md", content="x"), call("read_file", path="없는파일.txt")),
                   reply(content="저장하지 못했습니다")])
    list(Agent(settings, profile, llm=llm, approver=lambda r: False, workdir=folder).run("저장"))
    log = (folder / session.LOG_NAME).read_text(encoding="utf-8")
    assert "파일 저장: `a.md` — 거부함" in log and "파일 읽기: `없는파일.txt` — 실패" in log


def test_workdir_must_be_inside_writable_folder(settings, profile, tmp_path):
    with pytest.raises(SandboxError):
        Agent(settings, profile, llm=FakeLLM([]), workdir=tmp_path / "outside")


def test_prompt_mentions_session_folder(settings, profile, ws):
    folder = session.create_session_dir(ws, "x")
    prompt = Agent(settings, profile, llm=FakeLLM([]), workdir=folder).system_prompt()
    assert str(folder) in prompt and "이 대화 전용 폴더다" in prompt


# ───────────────────────── 관리 화면

@pytest.fixture
def client(monkeypatch, settings, tmp_path):
    settings.session_folders = True
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


def test_server_gives_each_conversation_a_folder(client, ws, monkeypatch):
    client.script[:] = [reply(content="1차")]
    first = post(client, "/api/v1/runs", {"task": "보고서 써줘", "profile": "general"}).json()["run_id"]
    run1 = wait(client, first)
    folder = run1["workdir"]
    assert folder and folder.startswith(str(ws)) and "보고서 써줘" in folder

    client.script[:] = [reply(content="2차")]
    second = post(client, "/api/v1/runs", {"task": "더 길게", "profile": "general", "parent_run_id": first}).json()["run_id"]
    assert wait(client, second)["workdir"] == folder                      # 이어서 하면 같은 폴더

    client.script[:] = [reply(content="새 대화")]
    other = post(client, "/api/v1/runs", {"task": "다른 일", "profile": "general"}).json()["run_id"]
    assert wait(client, other)["workdir"] != folder                       # 새 대화는 새 폴더

    opened = []
    monkeypatch.setattr(runs_api.os, "startfile", lambda p: opened.append(str(p)), raising=False)
    assert post(client, f"/api/v1/runs/{first}/open-folder").status_code == 200 and opened == [folder]

    assert client.delete(f"/api/v1/threads/{first}", headers={"X-Agent-Token": deps.TOKEN}).status_code == 200
    assert (ws / folder.split("\\")[-1] / session.LOG_NAME).exists()      # 대화를 지워도 파일은 남는다
