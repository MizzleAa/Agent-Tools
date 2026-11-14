import os
import subprocess

import pytest

from agent.config import AllowedDir
from agent.safety import Sandbox, SandboxError
from agent.tools.files import search_files


@pytest.fixture
def sandbox(settings):
    return Sandbox.from_settings(settings)


def test_relative_paths_inside_workspace_are_allowed(sandbox, ws):
    assert sandbox.resolve("notes/a.txt") == ws / "notes" / "a.txt"
    assert sandbox.resolve(".") == ws
    assert sandbox.resolve(str(ws / "x.txt"), write=True) == ws / "x.txt"


@pytest.mark.parametrize("path", [
    "..\\outside.txt",
    "../../etc",
    "sub\\..\\..\\outside",
    "C:\\Windows\\win.ini",
    "C:/Windows/System32",
    "\\\\server\\share\\file.txt",
    "//server/share",
    "\\\\?\\C:\\Windows\\win.ini",
    "\\\\.\\PhysicalDrive0",
    "C:foo.txt",
    "a.txt:hidden",
    "CON",
    "nul.txt",
    "sub/COM1",
    "PROGRA~1",
    "",
])
def test_escape_attempts_are_blocked(sandbox, path):
    with pytest.raises(SandboxError):
        sandbox.resolve(path)


def test_read_only_dir_allows_read_but_blocks_write(settings, tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    settings.allowed_dirs.append(AllowedDir(path=str(ro), mode="ro"))
    sb = Sandbox.from_settings(settings)
    assert sb.resolve(str(ro / "a.txt")) == ro / "a.txt"
    with pytest.raises(SandboxError, match="읽기 전용"):
        sb.resolve(str(ro / "a.txt"), write=True)


def test_ro_subfolder_inside_rw_folder_wins(settings, ws):
    (ws / "frozen").mkdir()
    settings.allowed_dirs.append(AllowedDir(path=str(ws / "frozen"), mode="ro"))
    sb = Sandbox.from_settings(settings)
    sb.resolve("other.txt", write=True)
    with pytest.raises(SandboxError):
        sb.resolve("frozen/a.txt", write=True)


def _make_junction(link, target) -> bool:
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    return r.returncode == 0


def test_junction_pointing_outside_is_blocked(sandbox, ws, tmp_path, ctx):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
    if not _make_junction(ws / "door", outside):
        pytest.skip("정션을 만들 수 없음")
    with pytest.raises(SandboxError):
        sandbox.resolve("door/secret.txt")
    # 검색할 때도 정션을 따라 들어가지 않아야 한다
    assert "SECRET" not in search_files(ctx, path=".", text="SECRET")


def test_symlink_pointing_outside_is_blocked(sandbox, ws, tmp_path):
    outside = tmp_path / "outside2.txt"
    outside.write_text("x", encoding="utf-8")
    try:
        os.symlink(outside, ws / "link.txt")
    except OSError:
        pytest.skip("심볼릭 링크 권한 없음 (개발자 모드 꺼짐)")
    with pytest.raises(SandboxError):
        sandbox.resolve("link.txt")
