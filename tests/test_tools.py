import threading
import time

import pytest

from agent.context import truncate_output
from agent.tools import REGISTRY, ToolError
from agent.tools.files import list_dir, read_file, search_files, write_file
from agent.tools.shell import run_command, run_python


def test_schema_hides_ctx_and_titles():
    params = REGISTRY["read_file"].schema()["function"]["parameters"]
    assert "ctx" not in params["properties"]
    assert params["required"] == ["path"]
    assert "title" not in str(params)


def test_read_file_ranges(ctx, ws):
    (ws / "a.txt").write_text("\n".join(f"줄{i}" for i in range(1, 11)), encoding="utf-8")
    out = read_file(ctx, path="a.txt", start_line=3, max_lines=2)
    assert "    3| 줄3" in out and "    4| 줄4" in out and "줄5" not in out
    assert "start_line=5" in out


def test_read_file_warns_only_if_head_never_read(ctx, ws):
    (ws / "s.csv").write_text("date,region\n1,서울\n2,부산\n", encoding="utf-8")
    assert "아직 읽지 않았습니다" in read_file(ctx, path="s.csv", start_line=2)   # 헤더를 건너뛰고 읽으면 경고
    read_file(ctx, path="s.csv")
    assert "아직 읽지 않았습니다" not in read_file(ctx, path="s.csv", start_line=3)   # 이미 처음부터 읽었으면 경고 없음


def test_read_file_decodes_cp949(ctx, ws):
    (ws / "k.txt").write_bytes("한국어 메모".encode("cp949"))
    assert "한국어 메모" in read_file(ctx, path="k.txt")


def test_read_file_rejects_binary_and_missing(ctx, ws):
    (ws / "b.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(ToolError):
        read_file(ctx, path="b.bin")
    with pytest.raises(ToolError):
        read_file(ctx, path="nope.txt")


def test_write_append_and_list(ctx, ws):
    write_file(ctx, path="r/out.md", content="a")
    write_file(ctx, path="r/out.md", content="b", append=True)
    assert (ws / "r" / "out.md").read_text(encoding="utf-8") == "ab"
    assert "[D] r/" in list_dir(ctx, path=".")


def test_search_by_name_and_text(ctx, ws):
    (ws / "src").mkdir()
    (ws / "src" / "login.py").write_text("x = 1\n# TODO: fix login\n", encoding="utf-8")
    (ws / "README.md").write_text("hello", encoding="utf-8")
    assert "login.py" in search_files(ctx, name_pattern="*.py")
    assert "README" not in search_files(ctx, name_pattern="*.py")
    assert "login.py:2:" in search_files(ctx, text="todo: FIX")


def test_run_command_korean_output_and_exit_code(ctx):
    out = run_command(ctx, command="Write-Output '한글 출력 테스트'; exit 3")
    assert "한글 출력 테스트" in out and "exit_code: 3" in out


def test_run_command_timeout_kills_process_tree(ctx):
    t0 = time.monotonic()
    out = run_command(ctx, command="ping -n 30 127.0.0.1", timeout_sec=2)
    assert "시간 초과" in out and time.monotonic() - t0 < 15


def test_run_command_cancel(ctx):
    threading.Timer(1.0, ctx.cancel.set).start()
    out = run_command(ctx, command="Start-Sleep -Seconds 20", timeout_sec=30)
    assert "사용자가 중지함" in out


def test_run_python(ctx):
    out = run_python(ctx, code="print(sum(range(1, 11)), '완료')")
    assert "55 완료" in out and "exit_code: 0" in out


def test_truncate_output_keeps_head_and_tail(tmp_path):
    text = "HEAD\n" + "중간\n" * 2000 + "TAIL 에러 메시지"
    out = truncate_output(text, 300, tmp_path / "full.txt")
    assert out.startswith("HEAD") and out.endswith("TAIL 에러 메시지") and "생략" in out
    assert (tmp_path / "full.txt").read_text(encoding="utf-8") == text
    assert truncate_output("짧음", 300) == "짧음"
