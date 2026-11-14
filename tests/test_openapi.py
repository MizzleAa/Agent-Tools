"""Swagger/OpenAPI 자동 읽기, 내부 주소 허용(allow_host), 파일 업로드 테스트.
FastAPI 처럼 /docs(JS 로 그려지는 페이지)와 /openapi.json 을 주는 가짜 사내 API 서버를 띄운다."""

import http.server
import json
import threading

import pytest
from fastapi.testclient import TestClient

import server.app as srv
from server import deps
from agent import config, connections, openapi
from agent.config import load_settings
from agent.loop import Agent
from agent.tools import ToolError
from agent.tools import web
from agent.tools.admin import add_api, allow_host
from agent.tools.api import api_send
from agent.tools.web import check_url, fetch_url
from tests.conftest import FakeLLM

DOCS_HTML = """<!DOCTYPE html><html><head><title>DatasetAPI - Swagger UI</title>
<link type="text/css" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger-ui"></div><script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>const ui = SwaggerUIBundle({ url: '/openapi.json', dom_id: '#swagger-ui' })</script></body></html>"""

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "DatasetAPI", "version": "1.2.0", "description": "딥러닝 데이터셋 관리"},
    "paths": {
        "/datasets": {
            "get": {"summary": "데이터셋 목록", "parameters": [
                {"name": "page", "in": "query"}, {"name": "project", "in": "query", "required": True}]},
            "post": {"summary": "데이터셋 만들기", "requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/DatasetIn"}}}}},
        },
        "/datasets/{dataset_id}": {
            "get": {"summary": "데이터셋 상세", "parameters": [{"name": "dataset_id", "in": "path", "required": True}]},
            "delete": {"summary": "데이터셋 삭제"},
        },
        "/datasets/{dataset_id}/upload": {
            "post": {"summary": "이미지 업로드", "requestBody": {"content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary"}, "split": {"type": "string"}}}}}}},
        },
    },
    "components": {"schemas": {"DatasetIn": {"type": "object", "required": ["name"],
                                             "properties": {"name": {"type": "string"}, "task": {"type": "string"}}}}},
}


class FakeApi:
    def __init__(self):
        self.uploads: list[bytes] = []
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def do_GET(self):
                if self.path == "/docs":
                    return self._send(200, DOCS_HTML, "text/html; charset=utf-8")
                if self.path == "/openapi.json":
                    return self._send(200, json.dumps(SPEC, ensure_ascii=False), "application/json")
                if self.path.startswith("/datasets"):
                    return self._send(200, json.dumps({"items": [{"id": 1, "name": "부품 결함"}]}, ensure_ascii=False), "application/json")
                return self._send(404, '{"detail":"Not Found"}', "application/json")

            def do_POST(self):
                api.uploads.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                return self._send(201, '{"ok": true}', "application/json")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake_api():
    s = FakeApi()
    web._pages.clear()
    yield s
    s.server.shutdown()


@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONNECTIONS_FILE", tmp_path / "connections.yaml")
    return tmp_path


@pytest.fixture
def loopback_ok(monkeypatch):
    """테스트 서버가 127.0.0.1 이라 '로컬 주소 금지' 규칙만 테스트에서 끈다 (규칙 자체는 아래에서 따로 검사)."""
    monkeypatch.setattr(connections, "_check_rule", lambda rule: None)


# ───────────────────────── 명세 해석

def test_endpoints_and_docs_text():
    eps = {(e["method"], e["path"]): e for e in openapi.endpoints(SPEC)}
    assert eps[("GET", "/datasets")]["query"] == ["page", "project*"]
    assert eps[("POST", "/datasets")]["body"] == ["name*", "task"]                       # $ref 를 따라간다
    assert eps[("POST", "/datasets/{dataset_id}/upload")]["form"] == ["file*(파일)", "split"]
    text = openapi.docs_text(SPEC)
    assert "GET /datasets?page&project*  데이터셋 목록" in text
    assert "form(파일 업로드): {file*(파일), split}" in text


def test_base_url_from_servers_and_swagger2():
    assert openapi.base_url({"servers": [{"url": "/api/v1"}]}, "http://h:9/openapi.json") == "http://h:9/api/v1"
    assert openapi.base_url({"swagger": "2.0", "host": "h:8", "basePath": "/v2"}, "http://x/swagger.json") == "http://h:8/v2"
    assert openapi.base_url({"openapi": "3.0.0"}, "http://h:9/docs/openapi.json") == "http://h:9"


# ───────────────────────── fetch_url 로 Swagger 페이지 읽기

def test_fetch_swagger_page_returns_api_summary(settings, ctx, fake_api):
    settings.web.allow_private_hosts = ["127.0.0.1"]
    out = fetch_url(ctx, url=f"{fake_api.url}/docs")
    assert "API 문서: DatasetAPI 1.2.0" in out and "엔드포인트 5개 (조회 2, 변경 3)" in out
    assert "POST /datasets/{dataset_id}/upload" in out and "add_api" in out


def test_blocked_message_tells_model_to_use_tools(settings):
    with pytest.raises(ToolError) as e:
        check_url(settings, "http://10.9.8.7:20000/docs")
    assert "allow_host(host='10.9.8.7')" in str(e.value) and "설정 수정을 부탁하지 말고" in str(e.value)


# ───────────────────────── add_api 로 문서 주소 하나만 주고 등록

def test_add_api_from_docs_url_reads_everything(settings, ctx, fake_api, loopback_ok):
    out = add_api(ctx, url=f"{fake_api.url}/docs")
    api = settings.apis[0]
    assert api.name == "datasetapi" and api.base_url == fake_api.url and api.allow_methods == ["GET"]
    assert api.docs_url == f"{fake_api.url}/docs" and "GET /datasets/{dataset_id}" in api.docs
    assert "엔드포인트 5개" in out and "변경 요청 허용" in out
    check_url(settings, f"{fake_api.url}/datasets")                         # 등록한 호스트는 자동 허용
    saved = load_settings(config.CONNECTIONS_FILE.parent / "none.yaml")
    assert saved.apis[0].docs_url.endswith("/docs") and saved.apis[0].managed


def test_add_api_without_docs_falls_back_to_plain(settings, ctx):
    out = add_api(ctx, url="http://mes.intra/api", name="mes", description="생산")
    assert settings.apis[0].base_url == "http://mes.intra/api" and "엔드포인트 목록을 모릅니다" in out


def test_refresh_and_write_toggle(settings, fake_api, loopback_ok):
    api, _ = connections.add_api_from_docs(settings, f"{fake_api.url}/docs", name="dl")
    assert connections.refresh_api_docs(settings, "dl") == 5
    assert connections.set_api_write("dl", True) == ["GET", "POST", "PUT", "PATCH", "DELETE"]
    assert load_settings(config.CONNECTIONS_FILE.parent / "none.yaml").apis[0].allow_methods[-1] == "DELETE"


# ───────────────────────── allow_host

def test_allow_host_normalizes_and_saves(settings, ctx):
    assert "허용 완료: 10.9.8.7" in allow_host(ctx, host="http://10.9.8.7:20000/docs")
    assert connections.allow_host(settings, "10.20.0.0/16") == "10.20.0.0/16"
    assert connections.allow_host(settings, "wiki.intra:8080/x") == "wiki.intra"
    loaded = load_settings(config.CONNECTIONS_FILE.parent / "none.yaml")
    assert {"10.9.8.7", "10.20.0.0/16", "wiki.intra"} <= set(loaded.web.allow_private_hosts)
    connections.remove_host("wiki.intra")
    assert "wiki.intra" not in load_settings(config.CONNECTIONS_FILE.parent / "none.yaml").web.allow_private_hosts


@pytest.mark.parametrize("bad", ["localhost", "127.0.0.1", "http://127.0.0.1:8765/", "0.0.0.0/0", "10.0.0.0/4", "169.254.169.254", "a b"])
def test_allow_host_rejects_dangerous(settings, bad):
    with pytest.raises(ValueError):
        connections.allow_host(settings, bad)


def test_allow_host_is_admin_permission():
    from agent.tools import REGISTRY
    assert REGISTRY["allow_host"].permission == "admin" and REGISTRY["add_api"].permission == "admin"


# ───────────────────────── 파일 업로드

def test_api_send_uploads_file_as_multipart(settings, ctx, ws, fake_api, loopback_ok):
    connections.add_api_from_docs(settings, f"{fake_api.url}/docs", name="dl")
    settings.apis[0].allow_methods = ["GET", "POST"]
    (ws / "img.png").write_bytes(b"\x89PNG fake")
    out = api_send(ctx, api="dl", method="POST", path="/datasets/7/upload", body='{"split": "train"}', file_path="img.png")
    body = fake_api.uploads[-1]
    assert "HTTP 201" in out and b'name="file"; filename="img.png"' in body and b"\x89PNG fake" in body
    assert b'name="split"' in body and b"train" in body
    with pytest.raises(Exception):
        api_send(ctx, api="dl", method="POST", path="/datasets/7/upload", file_path="..\\..\\secret.txt")


def test_find_endpoint_and_big_api_prompt(settings, ctx, profile, fake_api, loopback_ok, monkeypatch):
    from agent import loop
    from agent.tools.api import find_endpoint
    connections.add_api_from_docs(settings, f"{fake_api.url}/docs", name="dl")
    out = find_endpoint(ctx, query="upload image")
    assert out.splitlines()[0].startswith("[dl] POST /datasets/{dataset_id}/upload")
    assert "맞는 엔드포인트가 없습니다" in find_endpoint(ctx, query="zzzz")
    monkeypatch.setattr(loop, "PROMPT_DOC_LINES", 3)   # 엔드포인트가 많은 API 처럼
    profile.tools = ["api_get", "find_endpoint"]
    prompt = Agent(settings, profile, llm=FakeLLM([])).system_prompt()
    assert "경로 그룹: /datasets(5)" in prompt and "find_endpoint(api='dl'" in prompt
    assert "GET /datasets/{dataset_id}  데이터셋 상세" not in prompt


# ───────────────────────── 시스템 프롬프트의 '사내 연결 현황'

def test_prompt_shows_connections_even_when_empty(settings, profile):
    profile.tools = ["api_get", "add_api"]
    prompt = Agent(settings, profile, llm=FakeLLM([])).system_prompt()
    assert "## 사내 연결 현황" in prompt and "(등록된 API 없음" in prompt and "(등록된 사이트 없음)" in prompt


def test_prompt_lists_imported_api(settings, profile, fake_api, loopback_ok):
    connections.add_api_from_docs(settings, f"{fake_api.url}/docs")
    profile.tools = ["api_get"]
    prompt = Agent(settings, profile, llm=FakeLLM([])).system_prompt()
    assert "- datasetapi: DatasetAPI" in prompt and "    POST /datasets/{dataset_id}/upload" in prompt


# ───────────────────────── 관리 화면 API

def test_ui_add_api_hosts_and_toggle(monkeypatch, settings, temp_config, fake_api, loopback_ok):
    (temp_config / "settings.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(deps, "load_settings", lambda: load_settings(temp_config / "settings.yaml").model_copy(
        update={"data_dir": settings.data_dir}))
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    post = lambda path, body: c.post(path, json=body, headers={"X-Agent-Token": deps.TOKEN})   # noqa: E731
    r = post("/api/v1/apis", {"url": f"{fake_api.url}/docs"})
    assert r.status_code == 200 and r.json()["endpoints"] == 5
    assert c.put("/api/v1/apis/datasetapi/write-access", json={"allow": True}, headers={"X-Agent-Token": deps.TOKEN}).status_code == 200
    assert post("/api/v1/hosts", {"rule": "10.9.8.7"}).status_code == 200
    status = c.get("/api/v1/status").json()["intranet"]
    api = status["apis"][0]
    assert api["endpoints"] == 5 and "DELETE" in api["methods"] and api["docs_url"].endswith("/docs")
    assert {"rule": "10.9.8.7", "managed": True, "registered": False} in status["allowed_hosts"]
    assert c.delete("/api/v1/hosts", params={"rule": "10.9.8.7"}, headers={"X-Agent-Token": deps.TOKEN}).status_code == 200
