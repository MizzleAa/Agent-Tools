"""화면·채팅으로 사이트/API 를 등록·삭제하는 기능 테스트. 실제 config 폴더는 건드리지 않는다."""

import time

import pytest
import yaml
from fastapi.testclient import TestClient

import server.app as srv
from server import deps
from agent import config, connections, crawler
from agent.config import load_settings
from agent.events import ApprovalRequested, ToolResult
from agent.loop import Agent
from agent.tools import REGISTRY, ToolError
from agent.tools.admin import add_api, add_intranet_site
from agent.tools.web import check_url
from tests.conftest import FakeLLM, call, reply
from tests.test_intranet import Site


@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONNECTIONS_FILE", tmp_path / "connections.yaml")
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "secrets.yaml")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def site():
    s = Site()
    yield s
    crawler.cancel_jobs()
    crawler.wait_jobs(5)
    s.server.shutdown()


def wait_crawl(name, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = crawler.job_status(name)
        if st and st.state != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("수집이 끝나지 않음")


def test_add_source_scope_and_name(settings):
    src = connections.add_source(settings, "http://wiki.intra/display/PROC/Home")
    assert src.name == "wiki-display" and src.include_prefixes == ["http://wiki.intra/display/PROC/"]
    whole = connections.add_source(settings, "http://docs.intra", whole_site=True)
    assert whole.include_prefixes == ["http://docs.intra/"] and whole.start_urls == ["http://docs.intra/"]
    with pytest.raises(ValueError, match="이미 등록된"):
        connections.add_source(settings, "http://wiki.intra/display/PROC/Other")
    with pytest.raises(ValueError, match="http"):
        connections.add_source(settings, "file:///C:/x")


def test_saved_sources_are_loaded_and_removable(settings, temp_config):
    connections.add_source(settings, "http://wiki.intra/", name="wiki")
    (temp_config / "settings.yaml").write_text("intranet: {sources: []}\n", encoding="utf-8")
    loaded = load_settings(temp_config / "settings.yaml")
    assert [s.name for s in loaded.intranet.sources] == ["wiki"] and loaded.intranet.sources[0].managed
    assert "managed" not in (temp_config / "connections.yaml").read_text(encoding="utf-8")
    connections.remove_source("wiki")
    assert load_settings(temp_config / "settings.yaml").intranet.sources == []
    with pytest.raises(ValueError):
        connections.remove_source("wiki")


def test_settings_yaml_wins_on_name_clash(settings, temp_config):
    connections.add_source(settings, "http://a.intra/", name="wiki")
    (temp_config / "settings.yaml").write_text(
        "intranet:\n  sources:\n    - {name: wiki, start_urls: ['http://b.intra/']}\n", encoding="utf-8")
    loaded = load_settings(temp_config / "settings.yaml")
    assert len(loaded.intranet.sources) == 1 and loaded.intranet.sources[0].start_urls == ["http://b.intra/"]


def test_secret_is_saved_and_read(temp_config):
    connections.set_secret("MES_TOKEN", "abc123")
    assert config.get_secret("MES_TOKEN") == "abc123"
    assert yaml.safe_load((temp_config / "secrets.yaml").read_text(encoding="utf-8"))["MES_TOKEN"] == "abc123"
    with pytest.raises(ValueError):
        connections.set_secret("bad name!", "x")


def test_chat_add_site_needs_approval_then_saves_and_crawls(settings, profile, site):
    profile.tools = ["add_intranet_site", "search_intranet"]
    url = f"{site.url}/wiki/"
    llm = FakeLLM([reply(call("add_intranet_site", url=url, name="wiki", description="사내 위키")), reply(content="등록했습니다")])
    asked = []
    events = list(Agent(settings, profile, llm=llm, approver=lambda r: asked.append(r) or True).run(f"{url} 추가해줘"))
    assert asked and "범위" in asked[0].reason and REGISTRY["add_intranet_site"].permission == "admin"
    result = [e for e in events if isinstance(e, ToolResult)][0]
    assert result.ok and "수집을 백그라운드로 시작" in result.output
    assert "wiki" in config.CONNECTIONS_FILE.read_text(encoding="utf-8")
    st = wait_crawl("wiki")
    assert st.state == "done" and st.pages == 3
    check_url(settings, url)   # 같은 실행 안에서 바로 접근 가능해야 한다


def test_chat_add_site_denied_saves_nothing(settings, profile):
    profile.tools = ["add_intranet_site"]
    llm = FakeLLM([reply(call("add_intranet_site", url="http://wiki.intra/")), reply(content="취소")])
    list(Agent(settings, profile, llm=llm, approver=lambda r: False).run("추가"))
    assert not config.CONNECTIONS_FILE.exists() and settings.intranet.sources == []


def test_add_site_unreachable_is_saved_with_warning(settings, ctx):
    out = add_intranet_site(ctx, url="http://127.0.0.1:9/wiki/")
    assert "접속되지 않습니다" in out and len(settings.intranet.sources) == 1


def test_add_api_is_get_only_and_asks_for_token_in_ui(settings, ctx):
    out = add_api(ctx, name="MES", url="http://mes.intra/api/", description="생산 시스템",
                  docs="GET /lots/{id}  로트 상세", auth_secret="MES_TOKEN")
    api = settings.apis[0]
    assert api.name == "mes" and api.allow_methods == ["GET"] and api.base_url == "http://mes.intra/api"
    assert "상태 · 연결" in out   # 실제 화면 이름 (예전 안내 '상태 · 설정' 은 없는 화면이었다 — QA API-1)
    with pytest.raises(ToolError, match="이미"):
        add_api(ctx, name="mes", url="http://x.intra/", description="중복", docs="GET /a")


# ───────────────────────── 관리 화면 API

@pytest.fixture
def client(monkeypatch, settings, temp_config):
    (temp_config / "settings.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(deps, "load_settings", lambda: load_settings(temp_config / "settings.yaml").model_copy(
        update={"data_dir": settings.data_dir}))
    return TestClient(srv.app, base_url="http://127.0.0.1")


def post(c, path, body):
    return c.post(path, json=body, headers={"X-Agent-Token": deps.TOKEN})


def test_ui_add_and_delete_site(client, site):
    r = post(client, "/api/v1/intranet/sources", {"url": f"{site.url}/wiki/", "name": "wiki"})
    assert r.status_code == 200 and r.json()["name"] == "wiki"
    assert wait_crawl("wiki").pages == 3
    status = client.get("/api/v1/status").json()["intranet"]["sources"]
    assert status[0]["name"] == "wiki" and status[0]["pages"] == 3 and status[0]["managed"]
    assert client.delete("/api/v1/intranet/sources/wiki", headers={"X-Agent-Token": deps.TOKEN}).status_code == 200
    assert client.get("/api/v1/status").json()["intranet"]["sources"] == []
    assert post(client, "/api/v1/intranet/sources", {"url": "ftp://x"}).status_code == 400


def test_ui_secret_input_never_echoed(client):
    assert client.put("/api/v1/secrets/MES_TOKEN", json={"value": "s3cret"}, headers={"X-Agent-Token": deps.TOKEN}).status_code == 200
    assert config.get_secret("MES_TOKEN") == "s3cret"
    assert "s3cret" not in client.get("/api/v1/status").text
    assert client.put("/api/v1/secrets/X", json={"value": "y"}).status_code == 403   # 토큰 없는 요청 차단
