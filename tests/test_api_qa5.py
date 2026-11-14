"""API QA 5차(S05 · I01~I04) 결함 IMG-1·IMG-2·IMG-3 · S-10 · S-12 · DONE-1 · LONG-1 을 확인한다.

127.0.0.1 에 가짜 '앨범' API 를 띄운다 (로그인 · 앨범 목록 · 사진 목록(상태 필터) · 집계 · 빈 검토 목록 · 이름으로 미리보기 ·
ID 로 원본 · 자기 명세(openapi.json)). 등록은 '저장한 문서처럼 파라미터 정보가 없는 문서' 로 한다.
특정 사내 API 의 주소·경로·엔드포인트 이름·계정·지시서 내용은 쓰지 않는다.
"""

import http.server
import json
import threading
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from agent import apiauth, config, connections, imaging
from agent.events import Final, Images, Note, ToolResult
from agent.loop import IMAGE_ASK, Agent
from agent.profiles import load_profile
from agent.tools import ToolError
from agent.tools import api as api_tool
from agent.tools import web
from agent.tools.api import api_get, find_endpoint
from tests.conftest import FakeLLM, call, reply
from tests.test_api_qa import PASSWORD, USER, AuthServer
from tests.test_images import bmp

ALBUM = "0a1b2c3d-1111-4222-8333-444455556666"
PICS = [{"id": f"{i:08x}-aaaa-4bbb-8ccc-{i:012x}", "file_name": f"CAMX_{30000000 + i * 7:08d}_Q{i:02d}.bin",
         "status": "ng" if i % 3 else "ok", "note": "x" * 700} for i in range(1, 41)]
NG = [p for p in PICS if p["status"] == "ng"]
PNG = imaging._png(4, 3, [b"\x10\x20\x30\x40"] * 3, gray=True)


def album_spec(token_url: str) -> dict:
    sec = [{"OAuth2Password": []}]
    get = lambda s, q=(): {"get": {"summary": s, "tags": ["albums"], "security": sec,  # noqa: E731
                                   "parameters": [{"name": n, "in": "query"} for n in q]}}
    return {"openapi": "3.1.0", "info": {"title": "AlbumAPI", "version": "1"}, "paths": {
        "/albums": get("List albums", ("page", "size")),
        "/albums/{album_id}/pictures": get("List the pictures of this album (paged, filter by status)", ("status", "page", "size")),
        "/albums/{album_id}/pictures/stats": get("Per-status counts (ng/ok/total) of this album"),
        "/albums/{album_id}/reviews": get("Review queue of this album: pending / approved / rejected", ("page", "size")),
        "/albums/{album_id}/pictures/{file_name}/preview": get("Viewer-sized JPEG of a picture"),
        "/albums/{album_id}/pictures/{file_name}/detail": get("One picture's meta + marks"),
        "/pictures": get("Search pictures in the whole library (offset paged)", ("status", "offset", "limit")),
        "/pictures/{picture_id}/original": get("Original image bytes"),
        "/pictures/{picture_id}": get("One picture's metadata")},
        "components": {"securitySchemes": {"OAuth2Password": {"type": "oauth2", "flows": {
            "password": {"scopes": {}, "tokenUrl": token_url}}}}}}


class AlbumServer:
    def __init__(self, auth: AuthServer):
        me = self
        self.spec = album_spec(f"{auth.url}/auth/login")
        self.hits: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                me.hits.append(self.path)
                u = urlsplit(self.path)
                p, q = unquote(u.path), parse_qs(u.query)
                if p == "/openapi.json":
                    return self._send(200, me.spec)
                got = self.headers.get("Authorization", "")
                if not got.startswith("Bearer ") or got[7:] not in auth.valid:
                    return self._send(401, {"detail": "Not authenticated"})
                seg = p.strip("/").split("/")
                if p == "/albums":
                    return self._send(200, {"items": [{"id": ALBUM, "name": "album-a"}], "total": 1})
                if p == f"/albums/{ALBUM}/pictures":
                    items = [x for x in PICS if x["status"] == q["status"][0]] if "status" in q else PICS
                    return self._send(200, {"items": items[: int(q.get("size", ["50"])[0])], "total": len(items)})
                if p == f"/albums/{ALBUM}/pictures/stats":
                    return self._send(200, {"ng": len(NG), "ok": len(PICS) - len(NG), "total": len(PICS)})
                if p == f"/albums/{ALBUM}/reviews":
                    return self._send(200, {"items": [], "total": 0, "page": 1, "size": 50})
                if len(seg) == 5 and seg[:3] == ["albums", ALBUM, "pictures"] and seg[4] == "preview":
                    known = any(x["file_name"] == seg[3] for x in PICS)
                    return self._send(200, PNG, "image/png") if known else self._send(404, {"detail": "Not Found"})
                if p == "/pictures":   # 라이브러리 전체 검색: 모르는 파라미터(file_name)는 무시한다
                    return self._send(200, {"items": PICS[:20], "total": len(PICS)})
                if len(seg) == 3 and seg[0] == "pictures" and seg[2] == "original":
                    return self._send(200, bmp(64, 48), "image/bmp")
                return self._send(404, {"detail": "Not Found"})

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def servers():
    auth = AuthServer()
    api = AlbumServer(auth)
    web._pages.clear()
    api_tool._SPEC_PARAMS.clear()
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


def registered(settings, servers, name="albums", strip_params=True):
    """서버 명세로 등록한 뒤, 저장한 Swagger 화면으로 등록한 것처럼 파라미터 정보와 명세 주소를 지운다."""
    api, _ = servers
    connections.add_api_from_docs(settings, f"{api.url}/openapi.json", name=name)
    connections.set_api_credentials(settings, name, username=USER, password=PASSWORD)
    a = next(x for x in settings.apis if x.name == name)
    if strip_params:
        a.docs = "\n".join(line.split("?")[0] + ("  " + line.split("  ", 1)[1] if "  " in line else "")
                           for line in a.docs.splitlines())
        a.docs_url = ""
    return name


def intranet():
    p = load_profile("intranet")
    p.auto_approve_writes = True
    return p


def run(settings, script, task):
    agent = Agent(settings, intranet(), llm=FakeLLM(script), approver=lambda r: True)
    return agent, list(agent.run(task))


def notes(events, kind=None):
    return [n for n in events if isinstance(n, Note) and (kind is None or n.kind == kind)]


# ───────────────────────── IMG-2 · LONG-1: 파일명으로 이미지를 달라면 문서의 이름 경로를 바로

def test_IMG2_image_request_shows_documented_image_paths_name_based_first(settings, servers):
    name = registered(settings, servers)
    agent, _ = run(settings, [reply(content="끝")], f"앨범 사진 보여줘: {NG[0]['file_name']}")
    prompt = agent.messages[0]["content"]
    block = prompt.split("이미지 자체를 돌려주는 조회 경로", 1)[1]
    first, second = [line.strip() for line in block.splitlines()[1:3]]
    assert first.startswith("GET /albums/{album_id}/pictures/{file_name}/preview") and "/pictures/{picture_id}/original" in second
    assert "/detail" not in block.split("###")[0] and "/stats" not in block.split("###")[0]
    plain, _ = run(settings, [reply(content="끝")], "앨범 사진 몇 장이야?")
    assert "이미지 자체를 돌려주는 조회 경로" not in plain.messages[0]["content"]   # 이미지 요청이 아니면 붙이지 않는다


@pytest.mark.parametrize("task,image", [("불량 이미지 처음 3장 보여줘", True), ("이 이미지도 보여줘: A_1.bmp", True),
                                        ("X_20.png 띄워줘", True), ("불량 이미지 목록에서 처음 10장을 가져온다", False),
                                        ("이미지 개수 알려줘", False)])
def test_IMG2_image_request_detection(task, image):
    assert bool(IMAGE_ASK.search(task)) is image


def test_IMG2_LONG1_no_tool_ask_back_then_no_image_is_sent_back_then_partial(settings, servers):
    registered(settings, servers)
    excuse = "이 API 는 이미지를 UUID 로만 조회합니다. UUID 를 알려주시면 보여드리겠습니다."
    agent, events = run(settings, [reply(content=excuse), reply(content=excuse), reply(content=excuse)],
                        "이 이미지도 보여줘: CAMX_99999999_Q99.bin")
    assert [n.kind for n in notes(events)] == ["act", "act"]
    assert "먼저 시도하세요" in notes(events)[0].injected and "/preview" in notes(events)[1].injected
    final = events[-1]
    assert isinstance(final, Final) and final.status == "partial" and "표시된 이미지가 없습니다" in final.answer


def test_IMG2_real_and_missing_names_via_name_path(settings, servers):
    api, _ = servers
    name = registered(settings, servers)
    real, fake = NG[1]["file_name"], "CAMX_99999999_Q99.bin"
    script = [reply(call("api_get", api=name, path="/albums")),
              reply(call("api_get", api=name, path=f"/albums/{ALBUM}/pictures/{real}/preview"),
                    call("api_get", api=name, path=f"/albums/{ALBUM}/pictures/{fake}/preview")),
              reply(content=f"{real} 는 표시했고 {fake} 는 찾을 수 없습니다.")]
    _, events = run(settings, script, f"앨범의 사진 두 개 보여줘.\n- {real}\n- {fake}")
    shown = [it for e in events if isinstance(e, Images) for it in e.items]
    assert [(it["label"], it["ok"]) for it in shown] == [(real, True), (fake, False)] and "찾을 수 없음" in shown[1]["reason"]
    assert events[-1].status == "done"   # 한 장이라도 보였고, 없는 장은 실패로 보고했다


# ───────────────────────── DONE-1: 결과가 하나도 없으면 완료가 아니다

def test_DONE1_all_calls_without_any_response_is_partial(settings, servers, monkeypatch):
    name = registered(settings, servers)

    def timeout(*a, **k):
        raise ToolError("API 호출 실패 (ReadTimeout: timed out)")
    monkeypatch.setattr(api_tool, "exchange", timeout)
    script = [reply(call("api_get", api=name, path="/albums")), reply(content="서버가 응답하지 않습니다. 잠시 후 다시 해 보세요.")]
    _, events = run(settings, script, "앨범 목록 알려줘")
    assert events[-1].status == "partial" and "응답을 하나도 받지 못했습니다" in events[-1].answer


def test_DONE1_a_definite_404_answer_is_still_done(settings, servers):
    name = registered(settings, servers)
    script = [reply(call("api_get", api=name, path="/pictures/ffffffff-aaaa-4bbb-8ccc-000000000999")),
              reply(content="그 사진은 없습니다 (404).")]
    _, events = run(settings, script, "사진 ffffffff-aaaa-4bbb-8ccc-000000000999 있어?")
    assert events[-1].status == "done"   # 서버가 '없다'고 답한 것은 결과다 (응답이 하나도 없던 것과 다르다)


# ───────────────────────── IMG-1: 조건에 맞는 처음 N개

def test_IMG1_index_shows_each_items_category_in_order(settings, ctx, servers):
    name = registered(settings, servers)
    out = api_get(ctx, api=name, path=f"/albums/{ALBUM}/pictures")
    index = out.split("목록 색인", 1)[1].split("\n\n", 1)[0]
    assert "[status=…] 는 항목마다의 값" in index
    first_ng = [line for line in index.splitlines() if "[status=ng]" in line][:3]
    assert [line.split(") ", 1)[1].split(" [")[0] for line in first_ng] == [p["file_name"] for p in NG[:3]]


# ───────────────────────── S-10: 문서에 없는 파라미터

def test_S10_params_are_read_from_the_servers_own_spec_when_docs_have_none(settings, ctx, servers):
    api, _ = servers
    name = registered(settings, servers)
    with pytest.raises(ToolError) as e:
        api_get(ctx, api=name, path=f"/pictures?file_name={NG[0]['file_name']}")
    assert "문서에 없는 쿼리 파라미터입니다: file_name" in str(e.value) and "status" in str(e.value)
    assert "파라미터 출처: API 서버의 명세" in str(e.value) and not any("file_name=" in h for h in api.hits)
    found = find_endpoint(ctx, query="pictures list", api=name)
    assert "/pictures?status&page&size" in found and "API 서버의 명세에서 읽었다" in found


def test_S10_ignored_filter_is_reported_when_no_spec_is_reachable(settings, ctx, servers, monkeypatch):
    name = registered(settings, servers)
    monkeypatch.setattr(api_tool, "_server_params", lambda ctx, api: None)
    out = api_get(ctx, api=name, path=f"/pictures?file_name={NG[0]['file_name']}")
    assert f"'file_name={NG[0]['file_name']}' 로 걸렀는데 다른 값" in out and "무시했을 수 있습니다" in out


def test_S10_registered_from_a_server_spec_is_not_refetched(settings, ctx, servers):
    api, _ = servers
    name = registered(settings, servers, strip_params=False)
    api_get(ctx, api=name, path="/albums")
    assert sum("openapi.json" in h for h in api.hits) == 1   # 등록할 때 한 번뿐


# ───────────────────────── S-12: 빈 목록 하나로 '없다'고 단정하지 않게

def test_S12_empty_list_points_to_counts_before_concluding_none(settings, ctx, servers):
    name = registered(settings, servers)
    out = api_get(ctx, api=name, path=f"/albums/{ALBUM}/reviews")
    assert "빈 목록입니다" in out and "/albums/{album_id}/pictures/stats" in out and "해석이 불확실하면" in out
    again = api_get(ctx, api=name, path=f"/albums/{ALBUM}/reviews?page=2")
    assert "빈 목록입니다" not in again   # 같은 경로에는 한 번만
    full = api_get(ctx, api=name, path=f"/albums/{ALBUM}/pictures?status=ng")
    assert "빈 목록입니다" not in full


def test_S12_name_in_id_slot_404_is_not_absence(settings, ctx, servers):
    name = registered(settings, servers)
    out = api_get(ctx, api=name, path="/albums/album-a/pictures/stats")
    assert "HTTP 404" in out and "'album-a' 는 ID 자리({album_id})에 넣은 이름입니다" in out and "GET /albums 목록에서" in out
    real = api_get(ctx, api=name, path="/pictures/ffffffff-aaaa-4bbb-8ccc-000000000999")   # ID 처럼 생긴 값의 404 는 그대로
    assert "HTTP 404" in real and "ID 자리" not in real


def test_S12_profile_rule_mentions_concept_mapping_and_empty_results():
    prompt = load_profile("intranet").system_prompt
    assert "빈 결과·404 하나로 '없다'고 단정하지 말고" in prompt and "그 조건으로 거른 목록의 순서" in prompt


# ───────────────────────── IMG-3: ID 로 부른 이미지도 이름으로

def test_IMG3_image_called_by_id_is_labelled_with_the_name_from_earlier_results(settings, ctx, servers):
    name = registered(settings, servers)
    api_get(ctx, api=name, path=f"/albums/{ALBUM}/pictures?status=ng")
    out = api_get(ctx, api=name, path=f"/pictures/{NG[0]['id']}/original")
    entry = ctx.memo["images_new"][-1]
    assert entry["ok"] and entry["label"] == NG[0]["file_name"] and NG[0]["file_name"] in out
    owner = {"items": [{"id": "11111111-2222-4333-8444-555555555555", "name": "owner-x",
                        "owner_id": NG[1]["id"]}]}   # 다른 객체를 가리키는 owner_id 로 이름을 잘못 붙이지 않는다
    from agent import image_store
    image_store.remember_names(ctx.memo, owner)
    assert ctx.memo["id_names"].get(NG[1]["id"].lower()) == NG[1]["file_name"]
