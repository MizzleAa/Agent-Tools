"""모델 서버 포트 충돌 (예: Docker 의 nginx 가 8080 을 씀) 처리 테스트."""

import http.server
import json
import socket
import threading

import pytest

from agent import config
from agent.config import load_settings
from agent.runtime import free_port, health, port_in_use


class _Server:
    def __init__(self, handler_body, status=200, ctype="application/json"):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(handler_body.encode())

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONNECTIONS_FILE", tmp_path / "connections.yaml")
    return tmp_path


def _at(settings, port):
    settings.runtime.port = port
    settings.sync_local_backend()
    return settings


@pytest.mark.parametrize("body,status,ctype,expected", [
    (json.dumps({"status": "ok"}), 200, "application/json", "ok"),                       # llama-server
    (json.dumps({"error": {"code": 503, "message": "Loading model"}}), 503, "application/json", "loading"),
    ("<html><center>404 Not Found</center><hr><center>nginx</center></html>", 404, "text/html", "occupied"),   # Docker nginx
    ("<html>hello</html>", 200, "text/html", "occupied"),
])
def test_health_tells_llama_from_other_programs(settings, body, status, ctype, expected):
    srv = _Server(body, status, ctype)
    try:
        assert health(_at(settings, srv.port)) == expected
    finally:
        srv.server.shutdown()


def test_health_down_when_nothing_listens(settings):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert health(_at(settings, port)) == "down" and not port_in_use("127.0.0.1", port)


def test_free_port_skips_used_ones():
    srv = _Server("x")
    try:
        assert port_in_use("127.0.0.1", srv.port)
        assert free_port("127.0.0.1", srv.port) != srv.port
    finally:
        srv.server.shutdown()


def test_saved_port_moves_backend_url(temp_config):
    from agent import connections
    connections.set_runtime_port(18083)
    (temp_config / "settings.yaml").write_text("backend: {base_url: 'http://127.0.0.1:18080/v1'}\n", encoding="utf-8")
    s = load_settings(temp_config / "settings.yaml")
    assert s.runtime.port == 18083 and s.backend.base_url == "http://127.0.0.1:18083/v1"


def test_remote_backend_url_is_left_alone(temp_config):
    (temp_config / "settings.yaml").write_text("backend: {base_url: 'http://gpu-server.intra:8000/v1'}\n", encoding="utf-8")
    assert load_settings(temp_config / "settings.yaml").backend.base_url == "http://gpu-server.intra:8000/v1"
