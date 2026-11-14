"""관리 서버가 죽으면 llama-server 도 같이 죽게 하는 Windows Job 테스트. llama-server 대신 파이썬 자식 프로세스로 확인한다."""

import os
import subprocess
import sys
import time

import psutil
import pytest

from agent.runtime import KillOnCloseJob, LlamaServer

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows 전용")


def test_closing_job_kills_child_process():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    job = KillOnCloseJob()
    assert job.add(child.pid)
    job.close()   # 관리 서버 프로세스가 끝나는 상황과 같다
    child.wait(timeout=10)
    assert child.poll() is not None


def test_agent_commands_and_their_children_die_with_the_job(ctx, monkeypatch):
    """콘솔 강제 종료 = 관리 서버 프로세스가 끝나 Job 핸들이 닫히는 상황. run_command 가 띄운 것과 그 자식이 모두 끝나야 한다."""
    import threading
    import time as _time
    from agent import proc
    from agent.tools.shell import run_command

    job = KillOnCloseJob()
    monkeypatch.setattr(proc, "_job", job)
    result = {}
    t = threading.Thread(target=lambda: result.update(out=run_command(
        ctx, command=f"& '{sys.executable}' -c 'import time; time.sleep(60)'", timeout_sec=90)))
    t.start()
    me = psutil.Process()
    deadline = _time.monotonic() + 15
    spawned = []
    while _time.monotonic() < deadline:   # powershell(자식) → python(손자) 가 뜰 때까지
        spawned = [p for p in me.children(recursive=True) if p.name().lower().startswith(("powershell", "pwsh", "python"))]
        if len(spawned) >= 2:
            break
        _time.sleep(0.2)
    assert len(spawned) >= 2
    job.close()
    t.join(timeout=15)
    assert not t.is_alive() and "exit_code" in result["out"]
    gone, alive = psutil.wait_procs(spawned, timeout=10)
    assert not alive


def test_find_orphan_ignores_processes_that_are_not_ours(settings, tmp_path):
    settings.runtime.exe = str(tmp_path / "llama-server.exe")   # 이런 exe 로 도는 프로세스는 없다
    assert LlamaServer(settings).find_orphan() is None


def test_stop_kills_adopted_process(settings):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    server = LlamaServer(settings)
    server.adopted = psutil.Process(child.pid)
    assert server.running
    server.stop()
    child.wait(timeout=10)
    assert not server.running and server.adopted is None


def test_does_not_adopt_model_server_of_a_running_manager(settings, monkeypatch):
    """이미 켜진 관리 서버의 모델 서버는 이어받지 않는다 — 이어받고 바로 끝나면(포트 겹침) 같이 꺼진다 (2026-11-13).
    부모가 살아 있는 프로세스 = 다른 관리 서버의 것, 부모가 없으면 창을 닫아 남은 고아."""
    from agent import runtime
    me = psutil.Process()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        p = psutil.Process(child.pid)
        assert not runtime._owner_alive(p)                      # 띄운 것이 나 자신이면 '남의 것' 이 아니다
        monkeypatch.setattr(runtime.os, "getpid", lambda: -1)   # 다른 관리 서버(살아 있는 부모)가 띄운 것처럼
        assert runtime._owner_alive(p) and me.is_running()
        monkeypatch.setattr(runtime, "_owner_alive", lambda proc: True)
        monkeypatch.setattr(runtime.psutil, "process_iter", lambda attrs=None: iter([p]))
        monkeypatch.setattr(p, "info", {"exe": str(settings.path(settings.runtime.exe))}, raising=False)
        assert LlamaServer(settings).find_orphan() is None
    finally:
        child.kill()
        child.wait(timeout=10)


def test_second_start_only_opens_the_screen(monkeypatch, capsys):
    """관리 서버가 이미 켜져 있으면 또 띄우지 않는다 (모델 서버 이어받기 전에 멈춤) — 다른 프로그램이 포트를 쓰면 알리고 끝낸다."""
    import server.__main__ as entry
    real_check = entry.already_running
    opened, ran = [], []
    monkeypatch.setattr(entry.webbrowser, "open", opened.append)
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(sys, "argv", ["server", "--open"])
    monkeypatch.setattr(entry, "already_running", lambda port: "agent")
    entry.main()
    assert opened == ["http://127.0.0.1:8765"] and not ran and "이미 켜져" in capsys.readouterr().out
    monkeypatch.setattr(entry, "already_running", lambda port: "other")
    with pytest.raises(SystemExit, match="다른 프로그램"):
        entry.main()
    assert not ran
    assert real_check(1) == ""                        # 아무도 안 듣는 포트
