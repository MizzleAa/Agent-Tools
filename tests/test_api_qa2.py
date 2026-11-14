"""API QA 2차(A04~A07, B01) 결함 API-8~API-18 을 확인한다.

127.0.0.1 에 가짜 '주문' API(목록·집계·공개 경로·OAuth2 로그인)를 띄운다. 특정 사내 API 의 주소·경로·엔드포인트 이름·계정은 쓰지 않는다.
"""

import http.server
import json
import threading

import pytest
import yaml

from agent import apiauth, config, connections, openapi
from agent.context import truncate_output
from agent.events import Note, ToolResult
from agent.loop import Agent, unanswered_part
from agent.profiles import load_profile
from agent.tools import ToolError
from agent.tools import web
from agent.tools.admin import add_api
from agent.tools.api import api_get
from tests.conftest import FakeLLM, call, reply
from tests.test_api_qa import PASSWORD, USER, AuthServer

ITEMS = [{"id": i, "status": "ok" if i % 3 else "ng", "note": "x" * 60} for i in range(1, 121)]


def order_spec(token_url: str) -> dict:
    sec = [{"OAuth2Password": []}]
    get = lambda summary, tag, params=(): {"get": {"summary": summary, "tags": [tag], "security": sec,  # noqa: E731
                                                   "parameters": [{"name": n, "in": "query"} for n in params]}}
    paths = {"/healthz": {"get": {"summary": "Liveness probe", "tags": ["health"]}},
             "/orders": get("List orders", "orders", ("page", "size")),
             "/orders/{order_id}": get("Get order detail", "orders"),
             "/orders/{order_id}/items": get("List the items of this order (paged)", "items", ("page", "size")),
             "/orders/{order_id}/items/stats": get("Per-status counts (ok/ng/total) for this order", "items"),
             "/orders/{order_id}/tags": get("Tag list + per-tag item count for this order", "tags")}
    for k in range(12):   # 'item' 이 들어간 줄을 많이 만들어 결합 질의에서 'tag' 줄이 밀려나던 상황을 재현
        paths[f"/items/view{k}"] = get(f"Item image view {k} with status details", "items")
    return {"openapi": "3.1.0", "info": {"title": "OrderAPI", "version": "1.0"}, "paths": paths,
            "components": {"securitySchemes": {"OAuth2Password": {"type": "oauth2", "flows": {
                "password": {"scopes": {}, "tokenUrl": token_url}}}}}}


class OrderServer:
    def __init__(self, auth: AuthServer):
        me = self
        self.spec = order_spec(f"{auth.url}/auth/login")
        self.hits: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                me.hits.append(self.path)
                if self.path == "/openapi.json":
                    return self._send(200, me.spec)
                if self.path == "/healthz":
                    return self._send(200, {"ok": True})
                got = self.headers.get("Authorization", "")
                if not got.startswith("Bearer ") or got[7:] not in auth.valid:
                    return self._send(401, {"detail": "Not authenticated"})
                if self.path.startswith("/orders/7/items/stats"):
                    return self._send(200, {"ok": 80, "ng": 40, "total": 120})
                if self.path.startswith("/orders/7/items"):
                    return self._send(200, {"total": 120, "items": ITEMS})
                return self._send(200, {"items": [{"id": 7, "name": "order-7"}]})

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def servers():
    auth = AuthServer()
    api = OrderServer(auth)
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


def intranet():
    p = load_profile("intranet")
    p.auto_approve_writes = True
    return p


def registered(settings, servers, name="orders"):
    api, _ = servers
    connections.add_api_from_docs(settings, f"{api.url}/openapi.json", name=name)
    connections.set_api_credentials(settings, name, username=USER, password=PASSWORD)
    return name


# ───────────────────────── API-12. 결합 질의에서 관련 엔드포인트가 사라짐

def test_API12_combined_query_keeps_each_terms_best_lines():
    docs = openapi.docs_text(order_spec("/x"))
    for query in ("item stats tag", "tag", "tags", "tag count"):
        lines = openapi.search_docs(docs, query)
        assert any(line.startswith("GET /orders/{order_id}/tags") for line in lines), query
    assert openapi.search_docs(docs, "item stats tag")[0].startswith("GET /orders/{order_id}/items/stats")
    assert not openapi.search_docs(docs, "stat")[0].startswith("GET /items/view")   # 'stat' ≠ 'status'


# ───────────────────────── API-8. 목록을 페이지마다 세지 말고 집계 엔드포인트로

def test_API8_paging_through_a_list_points_to_aggregate_endpoints(settings, ctx, servers):
    name = registered(settings, servers)
    first = api_get(ctx, api=name, path="/orders/7/items?page=1&size=50")
    assert "집계 엔드포인트" in first and "/orders/{order_id}/items/stats" in first   # 긴 목록 첫 쪽에서 바로
    assert len(first) <= 4000                                                          # 루프의 자르기 한도 안
    again = api_get(ctx, api=name, path="/orders/7/items?page=2&size=50")
    assert "문서에서 찾은 후보" not in again                                             # 같은 경로엔 한 번만


def test_API8_A06_flow_answers_from_aggregate(settings, servers):
    name = registered(settings, servers)
    script = [reply(call("api_get", api=name, path="/orders/7/items?page=1&size=50")),
              reply(call("api_get", api=name, path="/orders/7/items/stats")),
              reply(content="주문 7: 정상 80개, 불량 40개 (총 120개)입니다.")]
    agent = Agent(settings, intranet(), llm=FakeLLM(script), approver=lambda r: True)
    events = list(agent.run("주문 7 의 불량·정상 개수 알려줘"))
    outs = [e.output for e in events if isinstance(e, ToolResult)]
    assert "/items/stats" in outs[0] and '"ng": 40' in outs[1] and events[-1].status == "done"


# ───────────────────────── API-9. 잘린 결과가 읽을 수 없는 파일을 가리킴

def test_API9_long_response_reads_on_with_offset_and_no_unreadable_path(settings, ctx, servers):
    name = registered(settings, servers)
    first = api_get(ctx, api=name, path="/orders/7/items")
    assert "이어 읽으려면 같은 path 로 offset=" in first and "data" + "/runs" not in first.replace("\\", "/")
    nxt = int(first.rsplit("offset=", 1)[1].split(".")[0])
    rest = api_get(ctx, api=name, path="/orders/7/items", offset=nxt)
    assert rest and "HTTP 200" not in rest                       # 같은 응답의 이어지는 부분 (다시 부르지 않음)


def test_API9_truncation_note_hides_unreadable_location(tmp_path):
    text = "A\n" + "중간\n" * 3000 + "끝"
    hidden = truncate_output(text, 300, tmp_path / "full.txt", readable=False)
    assert str(tmp_path) not in hidden and "다시 볼 수 없음" in hidden
    assert str(tmp_path / "full.txt") in truncate_output(text, 300, tmp_path / "full.txt")


# ───────────────────────── API-10. 같은 이유의 거부를 되풀이

def test_API10_same_refusal_escalates_then_stops(settings, profile):
    from agent.context import ARG_NOTE
    profile.auto_approve_writes = True
    leak = lambda i: reply(call("write_file", path=f"a{i}.py", content=ARG_NOTE.format(n=100 + i)))   # noqa: E731
    events = list(Agent(settings, profile, llm=FakeLLM([leak(i) for i in range(10)] + [reply(content="정리")]),
                        approver=lambda r: True, max_steps=0).run("t"))
    outs = [e.output for e in events if isinstance(e, ToolResult)]
    assert "번째 거부" not in outs[1] and "3번째 거부" in outs[2]
    assert len(outs) == 5 and events[-1].status == "loop"


# ───────────────────────── API-11. 사용자가 못 하는 일을 넘김

def test_API11_user_said_cannot_run_code(settings, ws):
    p = load_profile("coder")
    answer = "결과는 analyze.py 를 실행하시면 볼 수 있습니다."
    fixed = "도구로 직접 확인한 결과는 다음과 같습니다: 3건."
    events = list(Agent(settings, p, llm=FakeLLM([reply(content=answer), reply(content=fixed)])).run(
        "나는 파이썬 실행 같은 건 못 해. 개수 알려줘"))
    notes = [e for e in events if isinstance(e, Note) and e.kind == "handoff"]
    assert notes and "파이썬 실행 같은 건 못 해" in notes[0].injected and events[-1].answer == fixed


def test_API11_no_exec_tool_profile_cannot_hand_off_scripts(settings):
    answer = "작성해 둔 analyze.py 를 실행하시면 결함별 개수가 나옵니다."
    events = list(Agent(settings, intranet(), llm=FakeLLM([reply(content=answer)] * 2)).run("결함별 개수 알려줘"))
    assert [e.kind for e in events if isinstance(e, Note)] == ["handoff"]
    assert "사용자가 할 수 없는 일" in events[-1].answer and events[-1].status == "partial"


def test_API11_developer_running_code_is_fine(settings, ws):
    events = list(Agent(settings, load_profile("coder"), llm=FakeLLM([reply(content="python main.py 를 실행해 보세요.")])).run("실행법?"))
    assert not [e for e in events if isinstance(e, Note) and e.kind == "handoff"]


# ───────────────────────── API-13. 절반만 답했는데 done

@pytest.mark.parametrize("answer,partial", [
    ("불량 40개, 정상 80개입니다. 결함별 라벨 수는 확인할 수 없습니다.", True),
    ("총 개수는 알 수 없습니다.", True),
    ("I could not determine the per-tag counts.", True),
    ("인증이 설정되면 다시 /orders 를 호출하여 주문 수를 확인하실 수 있습니다.", True),   # 실제 모델 답 (B01 유형)
    ("계정을 넣으시면 바로 조회할 수 있습니다.", True),
    ("TODO 주석은 app/auth/login.py 에 있습니다. 다른 파일에서는 찾을 수 없었습니다.", False),
    ("context_length 값은 16384 입니다.", False),
    ("코드 블록: `확인할 수 없음` 은 예시 문구입니다.", False),
])
def test_API13_unanswered_detection(answer, partial):
    assert bool(unanswered_part(answer)) is partial


def test_API13_half_answer_is_partial_for_any_profile(settings):
    answer = "불량 40개, 정상 80개입니다. 태그별 개수는 확인할 수 없습니다."
    final = list(Agent(settings, load_profile("general"), llm=FakeLLM([reply(content=answer)])).run("두 가지 알려줘"))[-1]
    assert final.status == "partial" and "부분 완료" in final.answer


# ───────────────────────── API-14·15. 사용자가 준 문서 파일 · 출처

SAVED_UI = """<html><body><div class="information-container"><h1 class="title">OrderAPI<span><small><pre class="version"> 1.0 </pre></small></span></h1></div>
<button class="btn authorize unlocked">Authorize</button>
<h3 class="opblock-tag" id="operations-tag-health" data-tag="health"><span>health</span></h3>
<div class="opblock opblock-get" id="operations-health-h"><span class="opblock-summary-method">GET</span><span class="opblock-summary-path" data-path="/healthz"></span><div class="opblock-summary-description">Liveness probe</div></div>
<h3 class="opblock-tag" id="operations-tag-orders" data-tag="orders"><span>orders</span></h3>
<div class="opblock opblock-get" id="operations-orders-l"><span class="opblock-summary-method">GET</span><span class="opblock-summary-path" data-path="/orders"></span><div class="opblock-summary-description">List orders</div><button class="authorization__btn" aria-label="authorization button unlocked"></button></div>
<div class="opblock opblock-get" id="operations-orders-s"><span class="opblock-summary-method">GET</span><span class="opblock-summary-path" data-path="/orders/{order_id}/items/stats"></span><div class="opblock-summary-description">Per-status counts for this order</div><button class="authorization__btn" aria-label="authorization button unlocked"></button></div>
<div class="opblock opblock-delete" id="operations-orders-d"><span class="opblock-summary-method">DELETE</span><span class="opblock-summary-path" data-path="/orders/{order_id}"></span><div class="opblock-summary-description">Delete order</div><button class="authorization__btn" aria-label="authorization button unlocked"></button></div>
</body></html>"""


def test_API14_saved_swagger_screen_is_read_from_work_folder(settings, ctx, ws):
    (ws / "docs").mkdir()
    page = ws / "docs" / "OrderAPI - Swagger UI.html"
    page.write_text(SAVED_UI, encoding="utf-8")
    for ref in (str(page), page.as_uri(), "docs/OrderAPI - Swagger UI.html"):
        web._pages.clear()
        out = web.fetch_url(ctx, url=ref)
        assert "문서 파일:" in out and "엔드포인트 4개" in out and "GET /orders/{order_id}/items/stats" in out
        assert "자물쇠" in out and "3/4" in out and "GET /healthz" in out
    with pytest.raises(ToolError):
        web.fetch_url(ctx, url="C:/Windows/win.ini")                       # 허용 폴더 밖 파일은 못 읽는다


def test_API14_15_register_from_file_without_reading_server_docs(settings, ctx, ws, servers):
    api, auth = servers
    (ws / "OrderAPI.html").write_text(SAVED_UI, encoding="utf-8")
    out = add_api(ctx, url=api.url, docs_file="OrderAPI.html")
    reg = settings.apis[0]
    assert "출처: 사용자가 준 문서 파일" in out and "서버 문서는 읽지 않음" in out
    assert "/openapi.json" not in api.hits and "GET /orders/{order_id}/items/stats" in reg.docs
    assert "자물쇠" in out and reg.auth is None                               # 방식은 화면에 없다 → 관리 화면에서 정한다
    assert "인증 설정이 없습니다" in api_get(ctx, api=reg.name, path="/orders")  # 실현 가능한 다음 단계 안내


def test_API15_server_spec_source_is_named(settings, ctx, servers):
    api, _ = servers
    out = add_api(ctx, url=api.url)
    assert "출처: 서버의 API 명세" in out


# ───────────────────────── API-16·17. 같은 서버 계정 · 삭제 시 계정도

def test_API16_same_server_account_is_found_under_another_name(settings, servers, temp_config):
    api, _ = servers
    registered(settings, servers, name="first")
    connections.add_api_from_docs(settings, f"{api.url}/openapi.json", name="second")
    second = next(a for a in settings.apis if a.name == "second")
    assert not second.auth.missing() and "같은 서버에 저장돼 있던 계정" in second.auth.source


def test_API16_legacy_address_keys_are_found(settings, servers, temp_config):
    api, _ = servers
    base = connections.secret_base("", api.url)
    (temp_config / "secrets.yaml").write_text(yaml.safe_dump({f"{base}_USERNAME": USER, f"{base}_PASSWORD": PASSWORD}),
                                              encoding="utf-8")
    connections.add_api_from_docs(settings, f"{api.url}/openapi.json", name="SampleOrders")
    assert not settings.apis[0].auth.missing()


def test_API17_removing_a_registration_removes_its_account(settings, servers, temp_config):
    registered(settings, servers, name="first")
    api, _ = servers
    connections.add_api_from_docs(settings, f"{api.url}/openapi.json", name="second")   # 같은 계정을 같이 씀
    assert connections.remove_api("first") == []                                      # 다른 등록이 쓰므로 남긴다
    gone = connections.remove_api("second")
    left = yaml.safe_load((temp_config / "secrets.yaml").read_text(encoding="utf-8")) or {}
    assert gone and not any(k in left for k in gone) and PASSWORD not in json.dumps(left)


# ───────────────────────── API-18. 공개 경로 · 한자

def test_API18_public_endpoint_works_without_account(settings, ctx, servers):
    api, _ = servers
    connections.add_api_from_docs(settings, f"{api.url}/openapi.json", name="orders")
    out = api_get(ctx, api="orders", path="/healthz")
    assert "HTTP 200" in out and "계정 없이 조회됨" in out
    with pytest.raises(ToolError, match="계정이 저장되지 않았습니다"):
        api_get(ctx, api="orders", path="/orders")


def test_API18_answer_with_han_is_rewritten_once(settings):
    events = list(Agent(settings, intranet(), llm=FakeLLM([reply(content="방금 刚才 확인했습니다."),
                                                                       reply(content="방금 확인했습니다.")])).run("확인해줘"))
    assert [e.kind for e in events if isinstance(e, Note)] == ["language"] and events[-1].answer == "방금 확인했습니다."


# ───────────────────────── 계정·토큰이 모델·기록에 남지 않음 (2차 흐름 전체)

def test_secrets_never_reach_model_or_record(settings, servers):
    name = registered(settings, servers)
    script = [reply(call("api_get", api=name, path="/orders/7/items/stats")),
              reply(call("api_get", api=name, path="/orders/7/items?page=1&size=50")),
              reply(content="정상 80, 불량 40")]
    agent = Agent(settings, intranet(), llm=FakeLLM(script), approver=lambda r: True)
    events = list(agent.run("개수"))
    blob = json.dumps([e.to_dict() for e in events], ensure_ascii=False) + json.dumps(agent.messages, ensure_ascii=False)
    assert PASSWORD not in blob and USER not in blob and "tok-" not in blob
