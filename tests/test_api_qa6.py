"""API QA 6차(I05·I06·S06) 결함 S-13·S-14·S-15 를 확인한다.

특정 사내 API 의 주소·경로·엔드포인트 이름·계정·지시서 내용은 쓰지 않는다 (일반적인 가짜 '앨범' API — test_api_qa5 와 같은 서버).
"""

import pytest

from agent import grounding
from agent.evidence import Step
from agent.events import Final, Note
from agent.tools.api import api_get
from tests.conftest import FakeLLM, call, reply
from tests.test_api_qa5 import ALBUM, NG, intranet, registered, servers, temp_config  # noqa: F401 — 픽스처
from agent.loop import Agent


def ok(path, output, **args):
    return Step("api_get", {"api": "a", "path": path, **args}, True, output)


GROUPS = 'GET a/groups → HTTP 200 (application/json)\n\n[\n {"id": 1, "name": "alpha"},\n {"id": 2, "name": "beta"},\n' \
         ' {"id": 3, "name": "gamma"},\n {"id": 4, "name": "gamma_frame"}\n]'
LIST = 'GET a/albums/x/pictures?status=ng&size=3 → HTTP 200 (application/json)\n\n{"items": [{"file_name": "A_1.bin"}], "total": 1}'


# ───────────────────────── S-14: 성공한 단계를 실패로 보고

def test_S14_continuation_read_does_not_overwrite_the_calls_result():
    ledger = [ok("/albums/x/pictures?status=ng&size=3", LIST),
              ok("/albums/x/pictures?status=ng&size=3", "", offset=3500)]   # 끝까지 이어 읽은 빈 조각
    summary = grounding.results_summary(ledger)
    assert "→ 200:" in summary and "?:" not in summary
    assert "이어 읽기 offset=3500" in grounding.digest("api_get", ledger[1].args, True, "")


def test_S14_success_reported_as_failure_is_flagged():
    ledger = [ok("/albums/x/pictures?status=ng&size=3", LIST)]
    text = "4단계: NG 목록 조회 (`GET /albums/x/pictures?status=ng&size=3`) → 응답이 빈 값이나 실패했습니다."
    assert grounding.false_failures(text, ledger) == ["/albums/x/pictures?status=ng&size=3"]
    templ = "목록 조회 (`/albums/{album_id}/pictures?...&status=ng`) 실패로 10장을 얻을 수 없음"
    assert grounding.false_failures(templ, ledger)
    failed_too = ledger + [Step("api_get", {"api": "a", "path": "/albums/x/pictures?page=9"}, False, "오류: API 호출 실패 (ReadTimeout)")]
    assert grounding.false_failures(text, failed_too) == []      # 같은 경로가 실제로 실패한 적이 있으면 보지 않는다
    assert grounding.false_failures("목록 조회 성공 (`/albums/x/pictures?status=ng&size=3`)", ledger) == []


# ───────────────────────── S-13: 도구 결과와 다른 값

def test_S13_short_lists_keep_every_name_in_digests():
    d = grounding.digest("api_get", {"path": "/groups"}, True, GROUPS)
    assert "alpha, beta, gamma, gamma_frame" in d and "…" not in d


def test_S13_swapped_name_in_a_list_is_flagged():
    ledger = [ok("/groups", GROUPS)]
    bad = "- **데이터셋 그룹**: alpha, beta, gamma, album-a (4 개)"
    found = grounding.swapped_names(bad, ledger)
    assert found and found[0][0] == "album-a" and found[0][1] == "gamma_frame"
    assert grounding.swapped_names("- 그룹: alpha, beta, gamma, gamma_frame", ledger) == []
    assert grounding.swapped_names("- 비교: alpha, beta 그리고 다른 것", ledger) == []          # 이름 목록이 아닌 줄
    problems = grounding.check(bad, ledger, grounding.Seen())
    assert any("'album-a' 는" in p and "gamma_frame" in p for p in problems)


def test_S13_S14_in_a_run_report_is_sent_back_with_the_real_results(settings, servers):
    name = registered(settings, servers)
    wrong = (f"1단계 앨범: album-x (확인 완료)\n4단계 NG 목록 (`GET /albums/{ALBUM}/pictures?status=ng&size=3`) 조회 실패 — 응답이 빈 값")
    script = [reply(call("api_get", api=name, path="/albums")),
              reply(call("api_get", api=name, path=f"/albums/{ALBUM}/pictures?status=ng&size=3")),
              reply(content=wrong), reply(content=wrong), reply(content=wrong)]
    events = list(Agent(settings, intranet(), llm=FakeLLM(script), approver=lambda r: True).run("앨범 NG 사진 3장 목록 알려줘"))
    notes = [n for n in events if isinstance(n, Note) and n.kind == "grounding"]
    assert notes and "실패로 썼지만" in notes[0].injected and "'album-x'" not in notes[0].injected
    assert f"GET /albums/{ALBUM}/pictures?status=ng&size=3 → 200:" in notes[0].injected   # 실제로 받은 결과를 함께 보여 준다
    assert isinstance(events[-1], Final) and events[-1].status == "partial"


# ───────────────────────── S-15: 다른 개념으로 셈

def test_S15_counting_an_all_null_field_points_to_per_item_paths(settings, ctx, servers):
    name = registered(settings, servers)
    out = api_get(ctx, api=name, path=f"/albums/{ALBUM}/pictures?status=ng&size=5", count_by="label_category_id")
    assert "모두 비어 있거나 없습니다" in out and "항목마다" in out
    assert "/albums/{album_id}/pictures/{file_name}/detail" in out and "/preview" not in out.split("항목별 경로 후보")[1].split("\n\n")[0]
    fine = api_get(ctx, api=name, path=f"/albums/{ALBUM}/pictures?status=ng&size=5", count_by="status")
    assert "모두 비어 있거나 없습니다" not in fine


def test_S15_profile_rule():
    from agent.profiles import load_profile
    prompt = load_profile("intranet").system_prompt
    assert "모든 항목을 같은 상세 경로로 조회해" in prompt and "도구 결과에 있는 그대로" in prompt
