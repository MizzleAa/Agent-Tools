"""기능 '사내 API 이미지를 채팅 화면에 보여주기' (F1~F8) 를 확인한다.

127.0.0.1 에 가짜 '부품' API(로그인 필요 · BMP/PNG 이미지 · JSON 상세 · 없는 이름에 200 빈 결과 · 403 · SVG)를 띄운다.
특정 사내 API 의 주소·경로·엔드포인트 이름·계정은 쓰지 않는다.
"""

import http.server
import json
import struct
import threading
import zlib
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from agent import apiauth, config, connections, grounding, image_store, imaging
from agent.events import Final, Images, ToolResult
from agent.loop import Agent
from agent.profiles import load_profile
from agent.tools import web
from agent.tools.api import api_get
from agent.tools.base import Failed
from agent.tools.images import THUMB_SIDE, image_dir, show_images
from tests.conftest import FakeLLM, call, reply
from tests.test_api_qa import PASSWORD, USER, AuthServer


def bmp(width: int, height: int, bpp: int = 24, top_down: bool = False) -> bytes:
    """테스트용 비압축 BMP (8비트면 회색 팔레트)."""
    stride = ((width * bpp + 31) // 32) * 4
    palette = b"".join(bytes((i, i, i, 0)) for i in range(256)) if bpp == 8 else b""
    rows = []
    for y in range(height):
        if bpp == 8:
            row = bytes((x * 7 + y) % 256 for x in range(width))
        else:
            row = b"".join(bytes(((x * 3) % 256, (y * 5) % 256, 128)) for x in range(width))
        rows.append(row + b"\x00" * (stride - len(row)))
    pixels = b"".join(rows if top_down else rows[::-1])
    offset = 14 + 40 + len(palette)
    head = b"BM" + struct.pack("<IHHI", offset + len(pixels), 0, 0, offset)
    info = struct.pack("<IiiHHIIiiII", 40, width, -height if top_down else height, 1, bpp, 0, len(pixels), 2835, 2835,
                       256 if bpp == 8 else 0, 0)
    return head + info + palette + pixels


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack_from(">II", data, 16)


PARTS = {f"PART_{20260000 + i}_A{i}.bmp": bmp(640 + i, 480, 8 if i % 2 else 24) for i in range(1, 6)}
TINY_PNG = imaging._png(2, 2, [b"\x00\xff", b"\xff\x00"], gray=True)


def parts_spec(token_url: str) -> dict:
    sec = [{"OAuth2Password": []}]
    get = lambda s: {"get": {"summary": s, "tags": ["parts"], "security": sec}}  # noqa: E731
    paths = {"/parts": get("List parts"), "/parts/{name}": get("One part's meta (JSON)"),
             "/parts/{name}/picture": get("Original picture of a part (BMP)"),
             "/parts/{name}/icon": get("Small PNG icon"), "/parts/{name}/vector": get("Vector drawing (SVG)"),
             "/vault/{name}/picture": get("Restricted picture (admin)")}
    return {"openapi": "3.1.0", "info": {"title": "PartsAPI", "version": "1"}, "paths": paths,
            "components": {"securitySchemes": {"OAuth2Password": {"type": "oauth2", "flows": {
                "password": {"scopes": {}, "tokenUrl": token_url}}}}}}


class PartsServer:
    def __init__(self, auth: AuthServer):
        me = self
        self.spec = parts_spec(f"{auth.url}/auth/login")
        self.hits: list[str] = []
        self.headers: list[dict[str, str]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                me.hits.append(self.path)
                me.headers.append(dict(self.headers))
                p = unquote(urlsplit(self.path).path)
                if p == "/openapi.json":
                    return self._send(200, me.spec)
                got = self.headers.get("Authorization", "")
                if not got.startswith("Bearer ") or got[7:] not in auth.valid:
                    return self._send(401, {"detail": "Not authenticated"})
                parts = p.strip("/").split("/")
                if p == "/parts":
                    return self._send(200, {"items": [{"name": n} for n in PARTS]})
                if parts[0] == "vault":
                    return self._send(403, {"detail": "admin only"})
                name = parts[1] if len(parts) > 1 else ""
                if len(parts) == 2:
                    return self._send(200, {"name": name, "part_id": 1 if name in PARTS else None})
                if name not in PARTS:   # 없는 이름에도 200 + 빈 결과 (이미지 아님)
                    return self._send(200, {"name": name, "part_id": None, "size": 0})
                if parts[2] == "picture":
                    return self._send(200, PARTS[name], "image/bmp")
                if parts[2] == "icon":
                    return self._send(200, TINY_PNG, "image/png")
                if parts[2] == "vector":
                    return self._send(200, b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
                                      "image/svg+xml")
                return self._send(404, {"detail": "Not Found"})

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def servers():
    auth = AuthServer()
    api = PartsServer(auth)
    web._pages.clear()
    yield api, auth
    api.server.shutdown()
    auth.server.shutdown()


@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONNECTIONS_FILE", tmp_path / "connections.yaml")
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "secrets.yaml")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(connections, "_check_rule", lambda rule: None)
    apiauth._cache.clear()
    return tmp_path


def registered(settings, servers, name="parts"):
    api, _ = servers
    connections.add_api_from_docs(settings, f"{api.url}/openapi.json", name=name)
    connections.set_api_credentials(settings, name, username=USER, password=PASSWORD)
    return name


def intranet():
    p = load_profile("intranet")
    p.auto_approve_writes = True
    return p


NAMES = list(PARTS)


# ───────────────────────── 이미지 다루기 (외부 라이브러리 없이)

@pytest.mark.parametrize("bpp,top_down", [(24, False), (8, False), (24, True), (32, False), (4, False), (1, False)])
def test_bmp_is_shrunk_to_a_small_png(bpp, top_down):
    if bpp in (4, 1):
        stride = ((50 * bpp + 31) // 32) * 4
        pal = b"".join(bytes((i * 17 % 256, i * 17 % 256, i * 17 % 256, 0)) for i in range(1 << bpp))
        pixels = bytes(range(256)) * (stride * 40 // 256 + 1)
        off = 54 + len(pal)
        data = (b"BM" + struct.pack("<IHHI", off + stride * 40, 0, 0, off) +
                struct.pack("<IiiHHIIiiII", 40, 50, 40, 1, bpp, 0, stride * 40, 0, 0, 1 << bpp, 0) + pal + pixels[:stride * 40])
    else:
        data = bmp(900, 500, bpp if bpp != 32 else 24, top_down)
        if bpp == 32:   # 24비트 BMP 를 32비트로 바꿔 만든다
            data = _to32(900, 500)
    png = imaging.shrink_to_png(data, "bmp", THUMB_SIDE)
    w, h = png_size(png)
    assert max(w, h) <= THUMB_SIDE and w > 0 and h > 0
    raw = zlib.decompress(png[png.index(b"IDAT") + 4: png.index(b"IEND") - 8])
    assert len(raw) == h * (1 + w * (1 if png[25] == 0 else 3))   # 줄마다 필터 1바이트 + 픽셀


def _to32(width, height):
    stride = width * 4
    pixels = b"".join(bytes((x % 256, y % 256, 50, 255)) for y in range(height) for x in range(width))
    off = 54
    return (b"BM" + struct.pack("<IHHI", off + len(pixels), 0, 0, off) +
            struct.pack("<IiiHHIIiiII", 40, width, height, 1, 32, 0, len(pixels), 0, 0, 0, 0) + pixels)


def test_sniff_and_size():
    assert imaging.sniff(bmp(10, 5)) == "bmp" and imaging.size_of(bmp(10, 5), "bmp") == (10, 5)
    assert imaging.sniff(TINY_PNG) == "png" and imaging.size_of(TINY_PNG, "png") == (2, 2)
    assert imaging.sniff(b"<svg></svg>") == "" and imaging.sniff(b'{"a": 1}') == ""


# ───────────────────────── F1·F2·F3·F7: 받은 것만 그리고, 실패는 실패로

def test_show_images_fetches_saves_and_reports_without_bytes(settings, ctx, servers):
    api, _ = servers
    name = registered(settings, servers)
    paths = [f"/parts/{n}/picture" for n in NAMES[:3]] + ["/parts/NOPE_99999999_Z.bmp/picture", "/vault/x/picture",
                                                        f"/parts/{NAMES[0]}", f"/parts/{NAMES[0]}/vector",
                                                        f"/parts/{NAMES[0]}/icon"]
    out = show_images(ctx, api=name, paths=paths, labels=[NAMES[0], "엉뚱한 이름", NAMES[2]])
    items = ctx.memo["images_new"]
    assert [it["ok"] for it in items] == [True, True, True, False, False, False, False, True]
    assert items[0]["label"] == NAMES[0] and items[1]["label"] == NAMES[1]   # 경로에 없는 이름은 쓰지 않고 경로에서 뽑는다
    assert "찾을 수 없음" in items[3]["reason"] and "권한 없음" in items[4]["reason"]
    assert "이미지가 아닌 응답" in items[5]["reason"] and "이미지가 아닌 응답 (image/svg+xml)" in items[6]["reason"]
    folder = image_dir(ctx.run_dir)
    assert (folder / "1.orig.bmp").read_bytes() == PARTS[NAMES[0]]                     # F2: 받은 그대로
    assert max(png_size((folder / "1.thumb.png").read_bytes())) <= THUMB_SIDE          # F5: 작은 썸네일
    assert not (folder / "8.thumb.png").exists() and (folder / "8.orig.png").exists()  # 작은 PNG 는 원본이 곧 썸네일
    assert len(out) < 2000 and chr(0) not in out and "base64" not in out              # 모델에게는 글 몇 줄만 (바이트 없음)
    assert "이미지를 돌려주는 경로 후보" in out and "/picture" in out                   # JSON 경로였으면 이미지 경로를 알려 준다
    assert all(h.get("Authorization", "").startswith("Bearer ") for h, p in zip(api.headers, api.hits) if "picture" in p)


def test_show_images_limits_per_call(settings, ctx, servers):
    name = registered(settings, servers)
    paths = [f"/parts/{NAMES[i % 5]}/picture?v={i}" for i in range(15)]
    out = show_images(ctx, api=name, paths=paths)
    assert len(ctx.memo["images_new"]) == 12 and "3장은 붙이지 않음" in out


# ───────────────────────── 실행 안에서: 이벤트 · 지어낸 이름 · 컨텍스트 · 비밀

def test_api_get_attaches_images_and_never_returns_bytes(settings, ctx, servers):
    name = registered(settings, servers)
    out = api_get(ctx, api=name, path=f"/parts/{NAMES[0]}/picture")
    assert "채팅 화면에 붙였습니다" in out and NAMES[0] in out and len(out) < 600 and "BM" not in out.split("\n", 1)[1]
    entry = ctx.memo["images_new"][-1]
    assert entry["ok"] and entry["format"] == "bmp" and (image_dir(ctx.run_dir) / "1.thumb.png").exists()
    missing = api_get(ctx, api=name, path="/parts/NOPE_99999999_Z.bmp/picture")   # 없는 이름: 200 + 빈 JSON
    assert isinstance(missing, Failed) and "찾을 수 없음" in missing and ctx.memo["images_new"][-1]["ok"] is False
    denied = api_get(ctx, api=name, path="/vault/x/picture")
    assert isinstance(denied, Failed) and "권한 없음" in ctx.memo["images_new"][-1]["reason"]
    count = len(ctx.memo["images"])
    meta = api_get(ctx, api=name, path="/parts/NOPE_99999999_Z.bmp")             # 이미지에 '대한' JSON 경로는 칸을 만들지 않는다
    assert "part_id" in meta and len(ctx.memo["images"]) == count
    svg = api_get(ctx, api=name, path=f"/parts/{NAMES[0]}/vector")                # SVG 는 그림으로 받지 않는다 (스크립트가 들 수 있다)
    assert "<svg" in svg and len(ctx.memo["images"]) == count


def test_binary_non_image_response_is_not_decoded_into_text(settings, ctx, servers, monkeypatch):
    from agent.tools import api as api_tool
    name = registered(settings, servers)

    class R:
        status_code = 200
        content = b"PK\x03\x04\x00\x00" + bytes(range(256)) * 20
        headers = {"content-type": "application/octet-stream"}
        is_redirect = False
    monkeypatch.setattr(api_tool, "exchange", lambda *a, **k: (R(), False, {}))
    out = api_get(ctx, api=name, path="/parts")
    assert "이진 응답" in out and len(out) < 300


def test_images_event_and_context_in_a_run(settings, servers):
    api, _ = servers
    name = registered(settings, servers)
    made_up = NAMES[0].replace("_A1", "_A9")
    script = [reply(call("api_get", api=name, path="/parts")),
              reply(call("api_get", api=name, path=f"/parts/{NAMES[0]}/picture")),
              reply(call("api_get", api=name, path=f"/parts/{made_up}/picture")),
              reply(content=f"{NAMES[0]} 이미지를 붙였습니다. {made_up} 는 찾을 수 없습니다.")]
    agent = Agent(settings, intranet(), llm=FakeLLM(script), approver=lambda r: True)
    events = list(agent.run("부품 사진 보여줘"))
    shown = [e for e in events if isinstance(e, Images)]
    assert [it["ok"] for e in shown for it in e.items] == [True, False]
    assert "조회하지 않음" in shown[1].items[0]["reason"] and not any(made_up in h for h in api.hits)   # 지어낸 이름은 보내지 않는다
    results = [e for e in events if isinstance(e, ToolResult) and e.name == "api_get"]
    assert results[1].ok and len(results[1].output) < 600 and results[2].ok is False
    blob = json.dumps(agent.messages, ensure_ascii=False)
    assert "BM6" not in blob and "\\u0000" not in blob and len(blob) < 30000          # 이미지 바이트가 모델 입력에 없다
    dumped = json.dumps([e.to_dict() for e in events], ensure_ascii=False)
    assert PASSWORD not in dumped and USER not in dumped and "Bearer" not in dumped
    assert isinstance(events[-1], Final)


def test_show_images_batch_tool_in_a_custom_profile(settings, servers):
    name = registered(settings, servers)
    p = intranet()
    p.tools = [t for t in p.tools if t not in ("crawl_site",)] + ["show_images"]
    script = [reply(call("api_get", api=name, path="/parts")),
              reply(call("show_images", api=name, paths=[f"/parts/{n}/picture" for n in NAMES[:3]], labels=NAMES[:3])),
              reply(content="3장을 붙였습니다.")]
    events = list(Agent(settings, p, llm=FakeLLM(script), approver=lambda r: True).run("부품 사진 3장 보여줘"))
    shown = [e for e in events if isinstance(e, Images)]
    assert len(shown) == 1 and [it["label"] for it in shown[0].items] == NAMES[:3]


def test_intranet_profile_keeps_tool_count_and_says_when_to_show():
    from agent.profiles import MAX_TOOLS
    p = load_profile("intranet")
    assert len(p.tools) <= MAX_TOOLS and "show_images" not in p.tools
    assert "이미지를 보여 달라고 하면" in p.system_prompt and "개수·목록만 물으면 이미지 경로를 부르지 않는다" in p.system_prompt


# ───────────────────────── 서버: 받아 둔 파일만, 주소에 인증 정보 없이

def test_image_route_serves_cached_files_only(settings, ctx, servers, monkeypatch, tmp_path):
    from server import deps
    from server.app import app
    name = registered(settings, servers)
    run_dir = settings.data_path / "runs" / "20261106-120000-abcd"
    ctx.run_dir = run_dir
    show_images(ctx, api=name, paths=[f"/parts/{NAMES[1]}/picture", "/parts/NOPE_99999999_Z.bmp/picture"])
    monkeypatch.setattr(deps, "load_settings", lambda: settings)
    c = TestClient(app, base_url="http://127.0.0.1")
    base = "/api/v1/runs/20261106-120000-abcd/images"
    thumb = c.get(f"{base}/1/thumb")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/png"
    assert max(png_size(thumb.content)) <= THUMB_SIDE and "sandbox" in thumb.headers["content-security-policy"]
    view = c.get(f"{base}/1/view")   # 작은 BMP 는 브라우저가 그대로 그린다
    assert view.status_code == 200 and view.headers["content-type"] == "image/bmp"
    big = bmp(1900, 300)
    entry = image_store.record(ctx.memo, run_dir, name, "/parts/BIG_20260099_Z.bmp/picture", ok=True, data=big, fmt="bmp")
    big_view = c.get(f"{base}/{entry['id']}/view")   # 큰 원본은 긴 변 1600 PNG 로 줄여 보여 준다
    assert big_view.headers["content-type"] == "image/png" and max(png_size(big_view.content)) == 1600
    orig = c.get(f"{base}/1/original")
    assert orig.status_code == 200 and orig.content == PARTS[NAMES[1]] and orig.headers["content-type"] == "image/bmp"
    assert c.get(f"{base}/2/thumb").status_code == 404                     # 받지 못한 장은 파일이 없다
    assert c.get(f"{base}/1/other").status_code in (400, 404, 422)
    assert c.get("/api/v1/runs/..%2F..%2Fconfig/images/1/thumb").status_code == 404
    assert "/images/" not in json.dumps(app.openapi()["paths"])            # 이미지 바이트 경로는 JSON 계약에 넣지 않는다
