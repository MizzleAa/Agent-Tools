"""모델 프리셋 고르기 (입력창의 모델 선택) 테스트. 실제 llama-server 는 띄우지 않는다."""

import os
import time

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from server import deps
from agent import config, connections
from agent.config import ModelPreset, load_settings, model_file_state
from agent.runtime import build_argv


@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONNECTIONS_FILE", tmp_path / "connections.yaml")
    return tmp_path


def _old(path):   # 내려받기가 끝난 지 오래된 파일처럼 만든다
    t = time.time() - 120
    os.utime(path, (t, t))


@pytest.fixture
def presets(tmp_path):
    small, moe = tmp_path / "small.gguf", tmp_path / "moe.gguf"
    small.write_bytes(b"x")
    moe.write_bytes(b"x")
    _old(small)
    _old(moe)
    return [ModelPreset(name="small", file=str(small)),
            ModelPreset(name="moe", file=str(moe), context_length=32768, extra_args=["--n-cpu-moe", "36"])]


def test_apply_model_sets_runtime_and_backend(settings, presets):
    settings.models = presets
    assert settings.apply_model("moe")
    assert settings.backend.model == "moe" and settings.backend.context_length == 32768
    argv = build_argv(settings)
    assert argv[argv.index("-m") + 1].endswith("moe.gguf") and argv[-2:] == ["--n-cpu-moe", "36"]
    assert "--alias" in argv and argv[argv.index("--alias") + 1] == "moe"
    assert not settings.apply_model("없는모델")


def test_selected_model_is_remembered(settings, presets, temp_config):
    settings.models = presets
    connections.set_active_model(settings, "moe")
    (temp_config / "settings.yaml").write_text(
        "backend: {model: small}\nmodels:\n" + "".join(f"  - {{name: {m.name}, file: '{m.file}'}}\n" for m in presets),
        encoding="utf-8")
    assert load_settings(temp_config / "settings.yaml").backend.model == "moe"


def test_cannot_select_missing_or_downloading_file(settings, tmp_path):
    fresh = tmp_path / "fresh.gguf"
    fresh.write_bytes(b"x")                       # 방금 바뀐 파일 = 내려받는 중
    settings.models = [ModelPreset(name="gone", file=str(tmp_path / "no.gguf")), ModelPreset(name="fresh", file=str(fresh))]
    assert model_file_state(tmp_path / "no.gguf") == "missing" and model_file_state(fresh) == "downloading"
    with pytest.raises(ValueError, match="없습니다"):
        connections.set_active_model(settings, "gone")
    with pytest.raises(ValueError, match="내려받는 중"):
        connections.set_active_model(settings, "fresh")
    with pytest.raises(ValueError, match="목록에 없는"):
        connections.set_active_model(settings, "nope")


class FakeServer:
    def __init__(self, settings):
        self.settings, self.running, self.calls = settings, True, []
        self.proc, self.stage, self.error, self.notice = None, "", "", ""

    def stop(self):
        self.calls.append("stop")

    def start_async(self):
        self.calls.append(("start", self.settings.backend.model))

    def problems(self):
        return []


def test_select_endpoint_restarts_with_new_model(monkeypatch, settings, presets, temp_config):
    (temp_config / "settings.yaml").write_text(
        "backend: {model: small}\nmodels:\n" + "".join(f"  - {{name: {m.name}, file: '{m.file}'}}\n" for m in presets),
        encoding="utf-8")
    loader = lambda: load_settings(temp_config / "settings.yaml").model_copy(update={"data_dir": settings.data_dir})  # noqa: E731
    fake = FakeServer(loader())
    monkeypatch.setattr(deps, "load_settings", loader)
    monkeypatch.setattr(deps, "llm_server", fake)
    monkeypatch.setattr(deps, "runs", {})
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    r = c.put("/api/v1/models/active", json={"name": "moe"}, headers={"X-Agent-Token": deps.TOKEN})
    assert r.status_code == 200 and fake.calls == ["stop", ("start", "moe")]
    models = c.get("/api/v1/status").json()["models"]
    assert [m["name"] for m in models if m["active"]] == ["moe"] and all(m["state"] == "ready" for m in models)
    assert c.put("/api/v1/models/active", json={"name": "nope"}, headers={"X-Agent-Token": deps.TOKEN}).status_code == 400
