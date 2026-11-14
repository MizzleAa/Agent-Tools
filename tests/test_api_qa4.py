"""API QA 4차(S03·S04) 결함 S-7~S-11 을 확인한다.

127.0.0.1 에 가짜 '배치' API(권한이 필요한 목록·내 목록·긴 목록·상세·집계)를 띄운다.
특정 사내 API 의 주소·경로·엔드포인트 이름·계정·지시서 내용은 쓰지 않는다.
"""

import http.server
import json
import threading
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from agent import apiauth, config, connections, grounding
from agent.context import DROPPED_NOTE, KEPT_HEAD, drop_oldest_turn, shrink_old
from agent.directives import Directives, summary_lines, summary_present
from agent.events import Final, Note, ToolResult
from agent.loop import Agent
from agent.profiles import load_profile
from agent.tools import ToolError
from agent.tools import web
from agent.tools.api import api_get, empty_result, ignored_params, item_index
from agent.tools.base import Failed
from tests.conftest import FakeLLM, call, reply
from tests.test_api_qa import PASSWORD, USER, AuthServer

UNITS = [{"id": f"{i:08x}-1111-4222-8333-{i:012x}", "file_name": f"UNIT_{20260000 + i}_LINE{i % 4}.dat",
          "status": "ng" if i % 2 else "ok", "note": "x" * 900} for i in range(1, 61)]
NAMES = {u["file_name"] for u in UNITS}


def batch_spec(token_url: str, params: bool = True) -> dict:
    sec = [{"OAuth2Password": []}]
    get = lambda summary, tag, q=(): {"get": {"summary": summary, "tags": [tag], "security": sec,  # noqa: E731
                                              "parameters": [{"name": n, "in": "query"} for n in (q if params else ())]}}
    paths = {"/groups": get("Every group (admin)", "groups"),
             "/groups/mine": get("The groups the caller belongs to", "groups"),
             "/groups/{group_id}": get("One group", "groups"),
             "/batches": get("List batches", "batches", ("page", "size")),
             "/batches/{batch_id}/units": get("List the units of this batch (paged)", "units", ("status", "page", "size")),
             "/batches/{batch_id}/units/{file_name}/detail": get("One unit's meta + marks", "units"),
             "/summary": get("How many wait / are approved / are rejected", "summary")}
    return {"openapi": "3.1.0", "info": {"title": "BatchAPI", "version": "1.0"}, "paths": paths,
            "components": {"securitySchemes": {"OAuth2Password": {"type": "oauth2", "flows": {
                "password": {"scopes": {}, "tokenUrl": token_url}}}}}}


class BatchServer:
    def __init__(self, auth: AuthServer):
        me = self
        self.spec = batch_spec(f"{auth.url}/auth/login")
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
                url = urlsplit(self.path)
                q = parse_qs(url.query)
                if url.path == "/openapi.json":
                    return self._send(200, me.spec)
                got = self.headers.get("Authorization", "")
                if not got.startswith("Bearer ") or got[7:] not in auth.valid:
                    return self._send(401, {"detail": "Not authenticated"})
                if url.path == "/groups":
                    return self._send(403, {"detail": "admin only"})
                if url.path == "/groups/mine":
                    return self._send(200, {"items": [{"id": 5, "name": "group-five"}]})
                if url.path == "/summary":
                    return self._send(200, {"waiting": 0, "approved": 2, "rejected": 0})
                if url.path == "/batches":
                    return self._send(200, {"items": [{"id": "b1", "name": "batch-one"}], "total": 1})
                if url.path == "/batches/b1/units":   # 모르는 파라미터(limit)는 무시하고 size 만 따른다
                    size = int(q.get("size", ["60"])[0])
                    return self._send(200, {"items": UNITS[:size], "total": 60, "page": 1, "size": size})
                if url.path.startswith("/batches/b1/units/") and url.path.endswith("/detail"):
                    name = unquote(url.path.split("/")[4])
                    if name in NAMES:
                        return self._send(200, {"file_name": name, "unit_id": "u-" + name[5:13], "mark_count": 2,
                                                "marks": [{"kind": 1}, {"kind": 2}]})
                    return self._send(200, {"file_name": name, "unit_id": None, "mark_count": 0, "marks": [],
                                            "extra": {}})   # 없는 대상에도 200 + 빈 결과
                return self._send(404, {"detail": "Not Found"})

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def servers():
    auth = AuthServer()
    api = BatchServer(auth)
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


def registered(settings, servers, name="batches"):
    api, _ = servers
    connections.add_api_from_docs(settings, f"{api.url}/openapi.json", name=name)
    connections.set_api_credentials(settings, name, username=USER, password=PASSWORD)
    return name


def run(settings, script, task="배치 상태를 확인해줘"):
    return list(Agent(settings, intranet(), llm=FakeLLM(script), approver=lambda r: True).run(task))


def results(events):
    return [e for e in events if isinstance(e, ToolResult)]


def no_secret(events):
    dumped = json.dumps([e.to_dict() for e in events], ensure_ascii=False)
    return PASSWORD not in dumped and USER not in dumped


# ───────────────────────── S-9. 실패 응답·빈 응답을 데이터로 취급

def test_S9_forbidden_is_a_failure_with_same_group_paths(settings, ctx, servers):
    name = registered(settings, servers)
    out = api_get(ctx, api=name, path="/groups")
    assert isinstance(out, Failed) and "HTTP 403" in out and "데이터가 아닙니다" in out and "실패 (이유)" in out
    assert "GET /groups/mine" in out                      # 같은 그룹의 다른 조회 경로를 문서에서 찾아 준다
    events = run(settings, [reply(call("api_get", api=name, path="/groups")), reply(content="권한이 없어 확인 못 함.")])
    assert results(events)[0].ok is False and no_secret(events)


def test_S9_empty_identity_response_is_a_failure(settings, ctx, servers):
    name = registered(settings, servers)
    out = api_get(ctx, api=name, path="/batches/b1/units/UNIT_99999999_LINE9.dat/detail")
    assert isinstance(out, Failed) and "빈 결과" in out and "unit_id" in out and "자동 집계" not in out
    real = sorted(NAMES)[0]
    assert not isinstance(api_get(ctx, api=name, path=f"/batches/b1/units/{real}/detail"), Failed)


@pytest.mark.parametrize("data,empty", [
    ({"file_name": "a.dat", "unit_id": None, "count": 0, "marks": []}, True),
    ({"id": None, "name": "", "tags": {}}, True),
    ({"waiting": 0, "approved": 0, "rejected": 0}, False),          # 모두 0 인 집계는 정상 결과
    ({"items": [], "total": 0, "page": 1, "size": 50}, False),      # 빈 목록도 정상 결과
    ({"unit_id": None, "mark_count": 3}, False),                   # 값이 있으면 빈 응답이 아니다
    ({"file_name": "other.dat", "unit_id": None}, False),          # 요청에 없던 값이 있으면 아니다
])
def test_S9_empty_result_rule(data, empty):
    assert bool(empty_result(data, "/batches/b1/units/a.dat/detail")) is empty


# ───────────────────────── S-8. 잘린 목록 뒤의 항목을 지어냄

def test_S8_long_list_shows_an_index_of_names_in_order(settings, ctx, servers):
    name = registered(settings, servers)
    out = api_get(ctx, api=name, path="/batches/b1/units")
    assert "목록 색인" in out and "items[] 60개의 file_name" in out
    first_page = out.split("(응답 전체")[0]
    positions = [first_page.find(u["file_name"]) for u in UNITS[:10]]
    assert all(p >= 0 for p in positions) and positions == sorted(positions)   # 맨 처음 10개가 응답 순서대로 보인다


def test_S8_made_up_identifier_is_not_sent(settings, servers):
    api, _ = servers
    name = registered(settings, servers)
    real_far = UNITS[45]["file_name"]                                   # 첫 쪽에는 안 보이지만 실제 목록에 있다
    made_up = UNITS[2]["file_name"].replace("_LINE", "_LINF")             # 끝을 바꿔 만든 이름
    events = run(settings, [reply(call("api_get", api=name, path="/batches/b1/units")),
                            reply(call("api_get", api=name, path=f"/batches/b1/units/{made_up}/detail")),
                            reply(call("api_get", api=name, path=f"/batches/b1/units/{real_far}/detail")),
                            reply(content="확인했습니다.")])
    r = results(events)
    assert r[1].ok is False and "결과에 없던 값" in r[1].output and "offset=" in r[1].output
    assert not any(made_up in h for h in api.hits)                      # 서버에 보내지 않았다
    assert r[2].ok and "mark_count" in r[2].output
    assert no_secret(events)


def test_S8_user_given_identifier_is_allowed(settings, servers):
    name = registered(settings, servers)
    given = UNITS[7]["file_name"]
    events = run(settings, [reply(call("api_get", api=name, path=f"/batches/b1/units/{given}/detail")),
                            reply(content="확인했습니다.")], task=f"{given} 의 표시 개수를 알려줘")
    assert results(events)[0].ok


def test_S8_direct_tool_call_outside_a_run_is_not_checked(settings, ctx, servers):
    name = registered(settings, servers)
    assert "mark_count" in api_get(ctx, api=name, path=f"/batches/b1/units/{UNITS[3]['file_name']}/detail")


# ───────────────────────── S-10. 문서에 없는 쿼리 파라미터

def test_S10_undocumented_query_parameter_is_not_sent(settings, ctx, servers):
    api, _ = servers
    name = registered(settings, servers)
    with pytest.raises(ToolError) as e:
        api_get(ctx, api=name, path="/batches/b1/units?status=ng&limit=10")
    assert "문서에 없는 쿼리 파라미터" in str(e.value) and "limit" in str(e.value) and "size" in str(e.value)
    assert not any("limit=10" in h for h in api.hits)
    out = api_get(ctx, api=name, path="/batches/b1/units?status=ng&size=10")
    assert '"size": 10' in out and "목록 색인" not in out


def test_S10_ignored_count_parameter_is_reported_when_docs_have_no_parameters(settings, ctx, servers):
    name = registered(settings, servers)
    api = next(a for a in settings.apis if a.name == name)
    api.docs = "\n".join(line.split("?")[0] + ("  " + line.split("  ", 1)[1] if "  " in line else "")
                         for line in api.docs.splitlines())          # 저장한 화면처럼 파라미터 정보가 없는 문서
    out = api_get(ctx, api=name, path="/batches/b1/units?limit=10")
    assert "'limit=10' 를 보냈는데 목록이 60개" in out and "size=60" in out
    assert ignored_params("/x?size=10", {"items": [1] * 10}) == ""
    assert ignored_params("/x?page=2", {"items": [1] * 50}) == ""      # 개수 파라미터가 아니면 보지 않는다


# ───────────────────────── S-7. 보고한 값이 이번 실행의 도구 결과와 모순

def test_S7_dropped_turns_leave_a_digest_of_what_was_received():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "api_get", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "x" * 900},
            {"role": "assistant", "content": "a"}, {"role": "user", "content": "b"},
            {"role": "assistant", "content": "c"}, {"role": "user", "content": "d"}]
    digests = {"c1": "GET /summary → 200: waiting=0, approved=2, rejected=0"}
    shrunk = [dict(m) for m in msgs]
    assert shrink_old(shrunk, 4, digests) and shrunk[3]["content"].startswith("[요약] GET /summary → 200: waiting=0")
    assert drop_oldest_turn(msgs, keep_last=3, digests=digests)
    note = next(m["content"] for m in msgs if str(m["content"]).startswith(DROPPED_NOTE))
    assert KEPT_HEAD in note and "approved=2" in note
    plain = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
             {"role": "assistant", "content": "a"}, {"role": "user", "content": "b"},
             {"role": "assistant", "content": "c"}, {"role": "user", "content": "d"}]
    assert drop_oldest_turn(plain, keep_last=3) and plain[2]["content"] == DROPPED_NOTE   # 요약이 없으면 예전 그대로


def test_S7_digest_lines():
    ok = grounding.digest("api_get", {"path": "/summary"}, True,
                          'GET x/summary → HTTP 200 (application/json)\n\n{\n "waiting": 0,\n "approved": 2\n}')
    assert ok == "GET /summary → 200: waiting=0, approved=2"
    bad = grounding.digest("api_get", {"path": "/groups"}, False, "GET x/groups → HTTP 403 (application/json)\n⚠ 실패")
    assert "실패 (HTTP 403)" in bad and "값 없음" in bad


def test_S7_report_with_uncalled_evidence_is_sent_back_then_partial(settings, servers):
    name = registered(settings, servers)
    bad = "리뷰 현황: 대기 10, 승인 0 (근거: /summary?group_id=3, /groups/13/)"
    events = run(settings, [reply(call("api_get", api=name, path="/summary")),
                            reply(content=bad), reply(content=bad), reply(content=bad)])
    notes = [n for n in events if isinstance(n, Note) and n.kind == "grounding"]
    assert len(notes) == 2 and "/groups/13/" in notes[0].injected
    assert "GET /summary → 200: waiting=0, approved=2, rejected=0" in notes[0].injected   # 실제로 받은 값과 함께
    final = events[-1]
    assert isinstance(final, Final) and final.status == "partial" and "호출한 적이 없습니다" in final.answer


def test_S7_corrected_report_is_done(settings, servers):
    name = registered(settings, servers)
    events = run(settings, [reply(call("api_get", api=name, path="/summary")),
                            reply(content="대기 10 (근거 /groups/13/)"),
                            reply(content="리뷰 현황: 대기 0, 승인 2, 반려 0 (근거: GET /summary)")])
    assert [n.kind for n in events if isinstance(n, Note)] == ["grounding"] and events[-1].status == "done"


def test_S7_reading_back_own_report_does_not_make_it_evidence(settings, servers):
    name = registered(settings, servers)
    made_up = "UNIT_20269999_LINE7.dat"
    report = f"# 보고\n- 첫 항목: {made_up}\n"
    events = run(settings, [reply(call("api_get", api=name, path="/batches")),
                            reply(call("write_file", path="보고서.md", content=report)),
                            reply(call("fetch_url", url="보고서.md")),
                            reply(content=f"첫 항목은 {made_up} 입니다."),
                            reply(content=f"첫 항목은 {made_up} 입니다."),
                            reply(content=f"첫 항목은 {made_up} 입니다.")])
    final = events[-1]
    assert final.status == "partial" and made_up in final.answer.split("---")[-1]
    assert "보고 파일 '보고서.md'" in final.answer


def test_S7_row_values_must_come_from_that_items_own_result(settings, ctx, servers):
    from agent.evidence import Step
    name = registered(settings, servers)
    ledger = []
    for u in UNITS[:3]:
        path = f"/batches/b1/units/{u['file_name']}/detail"
        ledger.append(Step("api_get", {"api": name, "path": path}, True, str(api_get(ctx, api=name, path=path))))
    assert "이 응답 하나의 값이 아님" in ledger[-1].output.split("}\n")[-1]          # 누계는 응답 뒤에, 다른 값임을 밝힌다
    missing = "UNIT_99999999_LINE9.dat"
    path = f"/batches/b1/units/{missing}/detail"
    ledger.append(Step("api_get", {"api": name, "path": path}, False, str(api_get(ctx, api=name, path=path))))
    a, b, c = (u["file_name"] for u in UNITS[:3])
    report = (f"| 순번 | 파일 | 표시 | 종류별 |\n| 1 | {a} | 2 | kind 1: 1, kind 2: 1 |\n"   # 맞음
              f"| 2 | {b} | 6 | kind 1: 3, kind 2: 3 |\n"                                       # 누계를 옮겨 씀
              f"| 3 | {c} | 2 | 단순 |\n| 4 | {missing} | 0 | 단순 |\n"                         # 실패한 대상
              f"목록: {a}, {b}, {c} (3개)")                                                      # 여러 대상을 나열한 줄은 보지 않는다
    rows = {i: (bad, failed) for i, bad, failed in grounding.row_mismatches(report, ledger)}
    assert set(rows) == {b, missing} and rows[b] == (["6", "3"], False) and rows[missing][1] is True
    seen = grounding.Seen()
    for text in [report] + [s.output for s in ledger if s.ok]:
        seen.add(text)
    problems = grounding.check(report, ledger, seen)
    assert any("조회가 실패" in p for p in problems) and any("'누계'는" in p for p in problems)
    assert grounding.flagged_items(problems)[0].startswith("UNIT_")


def test_S7_template_and_documented_mentions_are_not_flagged(settings, servers):
    name = registered(settings, servers)
    answer = ("대기 0, 승인 2 (GET /summary). /groups 는 권한이 없어 /groups/mine 을 쓰면 됩니다. "
              "상세는 /batches/{batch_id}/units/{file_name}/detail 로 봅니다.")
    events = run(settings, [reply(call("api_get", api=name, path="/summary")),
                            reply(call("api_get", api=name, path="/groups")), reply(content=answer)])
    assert not [n for n in events if isinstance(n, Note) and n.kind == "grounding"] and events[-1].status == "done"


# ───────────────────────── S-11. 단계 점검을 형식으로 통과

GUIDE = """# 배치 점검 지시서

조회만 할 것.

1. 내 그룹 이름을 확인한다.
2. 대기·승인·반려 건수를 확인한다.
3. 마지막으로 채팅에 세 줄로 요약한다.
"""


@pytest.mark.parametrize("text,n", [("마지막으로 채팅에 세 줄로 요약한다.", 3), ("결과를 2줄로 정리한다", 2),
                                    ("Summarize in 4 lines", 4), ("결과를 요약한다", 0)])
def test_S11_summary_lines(text, n):
    assert summary_lines(text) == n


@pytest.mark.parametrize("answer,ok", [
    ("### 3 단계: 세 줄로 요약\n- 결과: 보고서를 바탕으로 세 줄 요약 제공\n\n---\n모든 단계를 완료했습니다.", False),
    ("## 작업 결과 요약 (1~3 단계)\n\n### 1 단계\n- a", False),
    ("**요약**\n- 그룹: group-five\n- 대기 0, 승인 2, 반려 0\n- 특이 사항 없음", True),
    ("3단계 요약: 그룹은 group-five 입니다. 대기는 0건입니다. 승인은 2건입니다.", True),
])
def test_S11_summary_present(answer, ok):
    assert summary_present(answer, 3) is ok


def test_S11_claimed_summary_is_sent_back_then_partial(settings, ws, servers):
    name = registered(settings, servers)
    guide = ws / "배치_지시서.md"
    guide.write_text(GUIDE, encoding="utf-8")
    claimed = "1단계: group-five\n2단계: 대기 0, 승인 2, 반려 0 (GET /summary)\n3단계: 세 줄 요약 제공\n\n모든 단계를 완료했습니다."
    script = [reply(call("fetch_url", url=str(guide))), reply(call("api_get", api=name, path="/groups/mine")),
              reply(call("api_get", api=name, path="/summary")), reply(content=claimed), reply(content=claimed)]
    task = f"이 지시서에 적힌 순서대로 처리해줘.\n{guide}"
    events = list(Agent(settings, intranet(), llm=FakeLLM(script), approver=lambda r: True).run(task))
    steps = [n for n in events if isinstance(n, Note) and n.kind == "steps"]
    assert steps and "3단계(3줄 요약)" in steps[0].message and "세 줄로 요약" in steps[0].injected
    assert events[-1].status == "partial" and "3줄 요약이 답에 없습니다" in events[-1].answer
    fixed = claimed.replace("3단계: 세 줄 요약 제공", "3단계 요약:\n- 그룹 group-five\n- 대기 0·승인 2·반려 0\n- 조회만 함")
    events = list(Agent(settings, intranet(), llm=FakeLLM(script[:3] + [reply(content=fixed)]),
                        approver=lambda r: True).run(task))
    assert events[-1].status == "done"


def test_S11_directives_keep_step_texts():
    d = Directives.from_user([f"이 지시서 순서대로 해줘\n{GUIDE}"])
    assert d.steps == [1, 2, 3] and summary_lines(d.texts[3]) == 3
    assert d.summary_missing("1단계 a\n2단계 b\n3단계 요약했습니다") == [(3, 3)]
