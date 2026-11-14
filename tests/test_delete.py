"""사이드바 '대화 삭제' 테스트: 이어서 한 실행까지 한꺼번에, 실행 중이면 거부."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from server import deps
from agent.events import Final, RunStarted
from agent.store import Store


@pytest.fixture
def setup(monkeypatch, settings, tmp_path):
    st = Store(tmp_path / "t.db")
    monkeypatch.setattr(deps, "store", st)
    monkeypatch.setattr(deps, "load_settings", lambda: settings)
    monkeypatch.setattr(deps, "runs", {})
    for rid, parent in [("root", ""), ("child", "root"), ("grandchild", "child"), ("other", "")]:
        st.record(rid, 0, RunStarted(rid, f"{rid} 작업", "general", "m", parent))
        st.record(rid, 1, Final("done", "끝", 1, 1, 1, 0.1))
        (settings.data_path / "runs" / rid).mkdir(parents=True)
        (settings.data_path / "runs" / rid / "call_1.txt").write_text("출력", encoding="utf-8")
    return st, settings, TestClient(srv.app, base_url="http://127.0.0.1")


def post(c, path, body):
    return c.post(path, json=body, headers={"X-Agent-Token": deps.TOKEN})


def test_thread_ids_follow_the_whole_chain(setup):
    st, _, _ = setup
    assert sorted(st.thread_ids("root")) == ["child", "grandchild", "root"]
    assert st.thread_ids("없음") == []


def test_delete_removes_chain_and_outputs_only(setup):
    st, settings, c = setup
    r = c.delete("/api/v1/threads/root", headers={"X-Agent-Token": deps.TOKEN})
    assert r.status_code == 200 and r.json()["deleted"] == 3
    assert [x["id"] for x in st.list_runs()] == ["other"]
    assert st.get_run("child") is None
    runs_dir = settings.data_path / "runs"
    assert not (runs_dir / "root").exists() and not (runs_dir / "grandchild").exists() and (runs_dir / "other").exists()


def test_cannot_delete_running_thread_or_missing(setup):
    _, _, c = setup
    deps.runs["child"] = SimpleNamespace(done=False)
    assert c.delete("/api/v1/threads/root", headers={"X-Agent-Token": deps.TOKEN}).status_code == 409
    assert c.delete("/api/v1/threads/없음", headers={"X-Agent-Token": deps.TOKEN}).status_code == 404
    assert c.delete("/api/v1/threads/other").status_code == 403   # 토큰 없는 요청 차단
