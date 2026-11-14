import zipfile

import pytest

from agent.runtime import LlamaServer, build_argv, health, server_root, unpack_runtime


@pytest.fixture
def fake_downloads(settings, tmp_path, monkeypatch):
    """runtime/downloads 대신 임시 폴더를 쓰게 한다 (실제 llama.cpp zip 은 건드리지 않음)."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr("agent.runtime.DOWNLOADS_DIR", str(downloads))
    settings.runtime.exe = str(tmp_path / "llama.cpp" / "llama-server.exe")
    return downloads


def test_unpack_extracts_zips_next_to_exe(settings, fake_downloads, tmp_path):
    with zipfile.ZipFile(fake_downloads / "llama.zip", "w") as zf:
        zf.writestr("llama-server.exe", "fake")
    with zipfile.ZipFile(fake_downloads / "cudart.zip", "w") as zf:
        zf.writestr("cudart64_13.dll", "fake")
    assert unpack_runtime(settings) == ["cudart.zip", "llama.zip"]
    assert (tmp_path / "llama.cpp" / "llama-server.exe").exists()
    assert (tmp_path / "llama.cpp" / "cudart64_13.dll").exists()


def test_unpack_rejects_zip_slip(settings, fake_downloads, tmp_path):
    with zipfile.ZipFile(fake_downloads / "evil.zip", "w") as zf:
        zf.writestr("../../escape.txt", "x")
    with pytest.raises(RuntimeError, match="허용되지 않는 경로"):
        unpack_runtime(settings)
    assert not (tmp_path / "escape.txt").exists()


def test_zip_present_means_exe_is_not_a_problem(settings, fake_downloads, tmp_path):
    settings.runtime.model_path = str(tmp_path / "m.gguf")
    (tmp_path / "m.gguf").write_bytes(b"x")
    assert LlamaServer(settings).problems() != []      # zip 도 exe 도 없음
    (fake_downloads / "llama.zip").write_bytes(b"")
    assert LlamaServer(settings).problems() == []      # zip 이 있으면 시작할 때 풀면 된다


def test_argv_has_essentials(settings):
    argv = build_argv(settings)
    joined = " ".join(argv)
    assert argv[0].endswith("llama-server.exe")
    assert "--jinja" in argv and "-np 1" in joined
    assert f"-c {settings.backend.context_length}" in joined
    assert f"--alias {settings.backend.model}" in joined
    assert "--cache-type-k q8_0 --cache-type-v q8_0" in joined


def test_extra_args_and_no_cache_quant(settings):
    settings.runtime.cache_type = ""
    settings.runtime.extra_args = ["--n-cpu-moe", "24"]
    argv = build_argv(settings)
    assert "--cache-type-k" not in argv and argv[-2:] == ["--n-cpu-moe", "24"]


def test_server_root_and_down_health(settings):
    settings.backend.base_url = "http://127.0.0.1:9/v1"
    assert server_root(settings) == "http://127.0.0.1:9"
    assert health(settings) == "down"


def test_missing_files_are_reported(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("agent.runtime.DOWNLOADS_DIR", str(tmp_path / "no-downloads"))
    settings.runtime.exe = str(tmp_path / "nope.exe")
    settings.runtime.model_path = str(tmp_path / "nope.gguf")
    problems = LlamaServer(settings).problems()
    assert len(problems) == 2
