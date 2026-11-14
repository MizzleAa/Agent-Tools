"""API QA 3차(B02·B03·S01·S02) 결함 S-1~S-6 을 확인한다.

일반적인 가짜 지시서·가짜 '주문' API 만 쓴다 (특정 API·특정 지시서의 내용은 쓰지 않는다).
"""

import json

import pytest

from agent import apiauth, config, connections
from agent.directives import Directives, numbered_steps, read_only_quote, steps_missing
from agent.events import Note, ToolCall, ToolResult
from agent.loop import Agent, ends_with_plan
from agent.profiles import load_profile
from agent.tools import ToolError
from agent.tools import web
from agent.tools.api import api_get, count_block, shorten_opaque
from tests.conftest import FakeLLM, call, reply
from tests.test_api_qa import PASSWORD, USER, AuthServer
from tests.test_api_qa2 import OrderServer

GUIDE = """# 주문 점검 작업 지시서

대상: 주문 API. **조회만 할 것 — 아무것도 바꾸지 말 것.**

1. 주문 목록에서 첫 주문의 이름을 확인한다.
2. 그 주문의 항목 상태별 개수를 확인한다.
3. 태그별 개수를 많은 순서로 확인한다.
   - 태그가 없으면 그 사실만 적는다.
4. 결과를 작업 폴더에 "주문_점검_보고서.md" 로 저장한다.
5. 마지막으로 채팅에 세 줄로 요약한다.
"""


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


def guide_file(ws):
    folder = ws / "지시서"
    folder.mkdir()
    path = folder / "주문_점검_작업지시서.md"
    path.write_text(GUIDE, encoding="utf-8")
    return path


def task_for(path):
    return f"작업 지시서 파일 만들어 놨어. 이 파일에 적힌 순서대로 처리해줘.\n{path}"


def tools_offered(llm, i):
    return {t["function"]["name"] for t in llm.calls[i]["tools"] or []}


# ───────────────────────── S-2. 지시 원문이 긴 실행 중에 사라짐

def test_S2_directives_parsing():
    d = Directives.from_user([task_for(r"C:\work\지시서\주문_점검_작업지시서.md")])
    assert d.wants_order and d.files[0].endswith("주문_점검_작업지시서.md") and not d.read_only
    assert numbered_steps(GUIDE) == [1, 2, 3, 4, 5] and "조회만" in read_only_quote(GUIDE)
    assert numbered_steps("1. 하나\n2. 둘") == []   # 두 단계는 점검하지 않는다
    assert d.refers_to("file:///C:/work/지시서/주문_점검_작업지시서.md")


def test_S2_guide_text_survives_context_compaction(settings, ws, servers):
    name = registered(settings, servers)
    path = guide_file(ws)
    settings.backend.context_length = 6000   # 작은 컨텍스트 → 긴 결과가 쌓이면 오래된 도구 결과가 줄어든다
    script = [reply(call("fetch_url", url=str(path)))]
    script += [reply(call("api_get", api=name, path=f"/orders/7/items?page={i}&size=50")) for i in range(1, 7)]
    script += [reply(content="정리")]
    llm = FakeLLM(script)
    agent = Agent(settings, intranet(), llm=llm, approver=lambda r: True, max_steps=0)
    events = list(agent.run(task_for(path)))
    assert [n for n in events if isinstance(n, Note) and n.kind == "pinned"]
    first_result = agent.messages[3]["content"] if len(agent.messages) > 3 else ""
    shrunk = "4. 결과를 작업 폴더에" not in first_result                     # 원래 도구 결과는 압축으로 잘렸다
    last_system = llm.calls[-1]["messages"][0]["content"]
    assert "4. 결과를 작업 폴더에 \"주문_점검_보고서.md\" 로 저장한다." in last_system   # 그래도 원문은 시스템 지침에 있다
    assert "조회만 할 것" in last_system and shrunk


def test_S2_plain_reference_docs_are_not_pinned(settings, ws, servers):
    (ws / "API.md").write_text("# API 설명\nGET /orders 주문 목록\n", encoding="utf-8")
    agent = Agent(settings, intranet(), llm=FakeLLM([reply(call("fetch_url", url="API.md")), reply(content="정리")]))
    events = list(agent.run("API.md 보고 무슨 API 인지 정리해줘"))
    assert not [n for n in events if isinstance(n, Note) and n.kind == "pinned"]


# ───────────────────────── S-3. 금지한 변경 시도 · 잠금 해제 요청

def test_S3_read_only_guide_removes_change_tools_and_blocks_them(settings, ws, servers):
    api, _ = servers
    name = registered(settings, servers)
    path = guide_file(ws)
    llm = FakeLLM([reply(call("fetch_url", url=str(path))),
                   reply(call("api_send", api=name, method="POST", path="/orders/7/items", body="{}")),
                   reply(content="1단계 order-7\n2단계 …\n3단계 …\n4단계 …\n5단계 요약")])
    events = list(Agent(settings, intranet(), llm=llm, approver=lambda r: True).run(task_for(path)))
    assert "api_send" in tools_offered(llm, 0) and "api_send" not in tools_offered(llm, 1)   # 원문을 읽은 뒤로 변경 도구가 빠진다
    blocked = [e for e in events if isinstance(e, ToolResult) and e.name == "api_send"][0]
    assert not blocked.ok and "조회만 할 것" in blocked.output
    assert not [h for h in api.hits if not h.startswith("/openapi")]                          # 서버에 아무것도 안 보냄


def test_S3_read_only_in_chat_hides_change_tools_from_the_start(settings, servers):
    llm = FakeLLM([reply(content="조회 결과입니다.")])
    list(Agent(settings, intranet(), llm=llm).run("주문 개수 알려줘. 조회만 하고 아무것도 바꾸지 마"))
    assert "api_send" not in tools_offered(llm, 0)


def test_S3_answer_asking_to_unlock_is_sent_back(settings, servers):
    ask = "POST 가 막혀 있습니다. 관리 화면에서 '변경 요청 허용'을 켜 주세요."
    llm = FakeLLM([reply(content=ask), reply(content="조회로 확인한 결과: 주문 1개.")])
    events = list(Agent(settings, intranet(), llm=llm).run("주문 몇 개야? 조회만 해"))
    assert [n.kind for n in events if isinstance(n, Note)] == ["read_only"]


# ───────────────────────── S-5. 사용자가 준 파일과 같은 이름의 산출물

def test_S5_same_name_copy_is_refused_but_report_and_edits_are_fine(settings, ws, servers):
    path = guide_file(ws)
    (ws / "README.md").write_text("old", encoding="utf-8")
    llm = FakeLLM([reply(call("fetch_url", url=str(path))),
                   reply(call("write_file", path="주문_점검_작업지시서.md", content="# 지어낸 지시서")),
                   reply(call("write_file", path=str(path), content="# 덮어쓰기")),
                   reply(call("write_file", path="주문_점검_보고서.md", content="# 보고서")),
                   reply(content="1단계\n2단계\n3단계\n4단계\n5단계")])
    events = list(Agent(settings, intranet(), llm=llm, approver=lambda r: True).run(task_for(path)))
    r = [e for e in events if isinstance(e, ToolResult) and e.name == "write_file"]
    assert not r[0].ok and "사용자가 준 파일" in r[0].output and not (ws / "주문_점검_작업지시서.md").exists()
    assert not r[1].ok and path.read_text(encoding="utf-8") == GUIDE                      # 고정한 지시 원본은 안 바뀐다
    assert r[2].ok and (ws / "주문_점검_보고서.md").exists()
    coder = load_profile("coder")
    coder.auto_approve_writes = True
    ok = list(Agent(settings, coder, llm=FakeLLM([reply(call("write_file", path="README.md", content="new")),
                                                  reply(content="고쳤습니다.")]), approver=lambda r: True).run("README.md 고쳐줘"))
    assert [e for e in ok if isinstance(e, ToolResult)][0].ok                              # 같은 파일을 고치는 건 된다


# ───────────────────────── S-4. 단계를 다 하지 않았는데 done

@pytest.mark.parametrize("text,plan", [
    ("…변경 후 다시 5 단계부터 작업을 수행해야 합니다.\n\n이제 최종 답변을 하겠습니다.", True),
    ("결과를 정리했습니다. 이제 보고서를 작성하겠습니다.", True),
    ("주문은 1개입니다. 필요하면 더 자세히 안내해 드리겠습니다.", False),
    ("주문은 1개입니다.", False),
    ("다음에 무엇을 할까요? 원하시면 정리하겠습니다?", False),
])
def test_S4_ends_with_plan(text, plan):
    assert ends_with_plan(text) is plan


def test_S4_plan_ending_after_tools_is_sent_back_then_partial(settings, servers):
    name = registered(settings, servers)
    ending = "주문 7 을 확인했습니다. 이제 최종 답변을 하겠습니다."
    events = list(Agent(settings, intranet(), llm=FakeLLM([reply(call("api_get", api=name, path="/orders")),
                                                         reply(content=ending), reply(content=ending)]),
                        approver=lambda r: True).run("주문 확인해줘"))
    assert [n.kind for n in events if isinstance(n, Note)] == ["act"] and events[-1].status == "partial"


def test_S4_missing_steps_are_sent_back_then_partial(settings, ws, servers):
    path = guide_file(ws)
    partly = "1단계: order-7\n2단계: 정상 80, 불량 40"
    events = list(Agent(settings, intranet(), llm=FakeLLM([reply(call("fetch_url", url=str(path))),
                                                         reply(content=partly), reply(content=partly)]),
                        approver=lambda r: True).run(task_for(path)))
    steps = [n for n in events if isinstance(n, Note) and n.kind == "steps"]
    assert steps and "3, 4, 5" in steps[0].message
    assert events[-1].status == "partial" and "결과가 없는 단계: 3, 4, 5" in events[-1].answer
    assert steps_missing("| 1 | a |\n| 2 | b |\n3단계 c\n4. d\n[5] e", [1, 2, 3, 4, 5]) == []


def test_S4_chat_steps_are_checked_too(settings):
    task = "아래 순서대로 해줘.\n1. 주문 확인\n2. 항목 수 확인\n3. 태그 확인\n4. 정리"
    events = list(Agent(settings, intranet(), llm=FakeLLM([reply(content="1. 주문 7\n2. 120개\n3. red 50\n4. 끝")])).run(task))
    assert events[-1].status == "done" and not [n for n in events if isinstance(n, Note) and n.kind == "steps"]


# ───────────────────────── S-1. 머릿속 집계

def test_S1_count_by_counts_exactly_anywhere_in_json():
    data = {"image": {"labels": [{"category_id": 4, "seg": "x" * 900}, {"category_id": 4}, {"category_id": 4},
                                 {"category_id": 2}, {"category_id": 4}, {"category_id": 2}]}}
    out = count_block(data, "category_id")
    assert "4 → 4개, 2 → 2개" in out and "합 6개" in out
    assert "응답에 이 필드가 없음" in count_block(data, "missing")
    short = shorten_opaque(data)["image"]["labels"][0]["seg"]
    assert short.endswith("(900자 생략)") and len(short) < 60
    assert shorten_opaque("a sentence with spaces " * 20) == "a sentence with spaces " * 20   # 문장은 그대로


def test_S1_auto_counts_and_running_totals_across_calls(ctx):
    from agent.config import ApiEndpoint
    from agent.tools.api import auto_counts, running_totals
    docs = "GET /items/{item_id}  Item detail with marks\nGET /items  List items"
    api = ApiEndpoint(name="x", base_url="http://h.intra", docs=docs)
    item = lambda k, marks: {"id": k, "name": f"{k}.bmp", "marks": [{"id": i, "category_id": c, "seg": "Z" * 500}  # noqa: E731
                                                                    for i, c in enumerate(marks)]}
    assert "marks[].category_id: 4 → 4개, 2 → 2개 (합 6개)" in auto_counts(item("a1", [4, 4, 4, 2, 4, 2]))
    assert "marks[].category_id: 1 → 1개 (합 1개)" in auto_counts(item("a2", [1]))            # 1개짜리도 센다
    assert "marks[].id" not in auto_counts(item("a1", [4, 4]))                                # 고유 id 는 세지 않는다
    assert running_totals(ctx, api, "/items/a1", item("a1", [4, 4, 4, 2, 4, 2])) == ""
    total = running_totals(ctx, api, "/items/a2", item("a2", [1, 4]))
    assert "응답 2개 (a1, a2)" in total and "4 → 5개" in total and "합 8개" in total
    again = running_totals(ctx, api, "/items/a2", item("a2", [1, 4]))                       # 같은 대상을 다시 받아도 두 번 세지 않는다
    assert "합 8개" in again


def test_S1_api_get_count_by(settings, ctx, servers):
    name = registered(settings, servers)
    out = api_get(ctx, api=name, path="/orders/7/items", count_by="status")
    assert "집계 (status): ok → 80개, ng → 40개 (합 120개" in out.split("\n\n", 2)[1]   # 응답 앞쪽에 (잘리지 않게)


# ───────────────────────── S-6. 지어낸 경로 · 관련 없는 도구 · 틀린 저장 경로

def test_S6_undocumented_path_is_not_sent(settings, ctx, servers):
    api, _ = servers
    name = registered(settings, servers)
    with pytest.raises(ToolError, match="문서에 없는 경로") as e:
        api_get(ctx, api=name, path="/me")
    assert "/me" not in api.hits and "GET /orders" in str(e.value)
    assert "HTTP 200" in api_get(ctx, api=name, path="/orders/7/items/stats")


def test_S6_site_search_is_hidden_without_sites(settings):
    llm = FakeLLM([reply(content="답")])
    list(Agent(settings, intranet(), llm=llm).run("질문"))
    assert not {"search_intranet", "crawl_site"} & tools_offered(llm, 0) and "fetch_url" in tools_offered(llm, 0)


def test_S6_wrong_saved_path_in_answer_is_noted(settings, ws):
    p = load_profile("general")
    p.auto_approve_writes = True
    answer = f"저장했습니다: {ws.parent}\\잘린폴더\\결과.md"
    final = list(Agent(settings, p, llm=FakeLLM([reply(call("write_file", path="결과.md", content="x")), reply(content=answer)]),
                       approver=lambda r: True).run("결과.md 로 저장해줘"))[-1]
    assert "저장 경로가 실제와 다릅니다" in final.answer and str(ws / "결과.md") in final.answer


# ───────────────────────── 계정·토큰이 모델·기록에 남지 않음

def test_secrets_stay_out_with_pinned_guide(settings, ws, servers):
    name = registered(settings, servers)
    path = guide_file(ws)
    agent = Agent(settings, intranet(), llm=FakeLLM([reply(call("fetch_url", url=str(path))),
                                                     reply(call("api_get", api=name, path="/orders/7/items/stats")),
                                                     reply(content="1단계\n2단계\n3단계\n4단계\n5단계")]),
                  approver=lambda r: True)
    events = list(agent.run(task_for(path)))
    blob = json.dumps([e.to_dict() for e in events], ensure_ascii=False) + json.dumps(agent.messages, ensure_ascii=False)
    assert PASSWORD not in blob and USER not in blob and not [e for e in events if isinstance(e, ToolCall) and e.name == "api_send"]
