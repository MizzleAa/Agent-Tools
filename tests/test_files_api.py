"""작업 폴더 보기 API (읽기 전용): 폴더 목록 · 파일 트리 · 파일 내용 · 폴더 밖 차단."""

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from agent.session import LOG_NAME
from server import deps


@pytest.fixture
def client(monkeypatch, settings, ws):
    monkeypatch.setattr(deps, "load_settings", lambda: settings)
    a = ws / "20261106-1000_합계"
    (a / "src").mkdir(parents=True)
    (a / LOG_NAME).write_text("# 합계 계산기\n\n내용\n", encoding="utf-8")
    (a / "src" / "core.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (a / ".venv" / "Lib").mkdir(parents=True)
    (a / ".venv" / "Lib" / "big.py").write_text("x", encoding="utf-8")
    (a / "logo.bin").write_bytes(b"\x89PNG\x00\x00\x01")
    (a / "메모.txt").write_bytes("한국어 메모".encode("cp949"))
    (ws / "scratch").mkdir()
    (ws / "notes.txt").write_text("폴더가 아님", encoding="utf-8")
    (ws.parent / "secret.txt").write_text("밖", encoding="utf-8")
    return TestClient(srv.app, base_url="http://127.0.0.1")


def test_folders_lists_work_folders_with_titles(client):
    folders = client.get("/api/v1/workspace/folders").json()
    assert [f["name"] for f in folders] == ["20261106-1000_합계", "scratch"]   # 작업기록 있는 것 먼저, 파일은 빼고
    assert folders[0]["title"] == "합계 계산기" and folders[0]["has_log"] and not folders[1]["has_log"]
    assert folders[1]["title"] == "scratch"


def test_tree_skips_heavy_folders(client):
    tree = client.get("/api/v1/workspace/folders/20261106-1000_합계/tree").json()
    paths = {e["path"]: e for e in tree["entries"]}
    assert paths["src"]["dir"] and paths["src/core.py"]["size"] > 0
    assert paths[".venv"]["skipped"] and not any(p.startswith(".venv/") for p in paths)
    assert not tree["truncated"]


def test_file_content_language_binary_and_encoding(client):
    url = "/api/v1/workspace/folders/20261106-1000_합계/file"
    code = client.get(url, params={"path": "src/core.py"}).json()
    assert code["language"] == "python" and code["text"].startswith("def add") and not code["binary"]
    assert client.get(url, params={"path": "logo.bin"}).json()["binary"]
    assert client.get(url, params={"path": "메모.txt"}).json()["text"] == "한국어 메모"   # cp949 로 저장된 파일


@pytest.mark.parametrize("folder,path", [
    ("20261106-1000_합계", "../notes.txt"), ("20261106-1000_합계", "../../secret.txt"),
    ("20261106-1000_합계", "C:/Windows/win.ini"), ("..", "secret.txt"), ("notes.txt", "x"), ("없음", "a.py"),
    ("20261106-1000_합계", "src"),
])
def test_outside_or_missing_is_404(client, folder, path):
    r = client.get(f"/api/v1/workspace/folders/{folder}/file", params={"path": path})
    assert r.status_code == 404


def test_tree_of_missing_folder_is_404(client):
    assert client.get("/api/v1/workspace/folders/없음/tree").status_code == 404
