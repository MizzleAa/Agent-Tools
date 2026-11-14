"""9단계 예약 실행: 정해 둔 시각 · 간격에 요청을 실행한다 (대화 화면 '/예약' · 사이드바 '예약').

시각 규칙(한 번 · 매일 · 평일 · 매주 · N분마다)과 다음 실행 시각, API(내 것만), 실행(그 사람으로 · 바쁘면 건너뜀),
놓친 시각(서버가 꺼져 있던 동안) 처리를 가짜 모델로 시험한다 — 진짜 시간은 기다리지 않는다 (tick(now)).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent import schedules
from server.api import schedules as sched_api
from tests.conftest import reply
from tests.test_multiuser import H, admin, client, env, issue, wait_done  # noqa: F401  (admin · env 는 fixture)

SAT = datetime(2026, 11, 14, 8, 30)   # 토요일 08:30


def test_when_rules_next_fire_and_text():
    nf = schedules.next_fire
    assert nf({"kind": "daily", "at": "09:00"}, SAT) == datetime(2026, 11, 14, 9, 0)
    assert nf({"kind": "daily", "at": "08:00"}, SAT) == datetime(2026, 11, 15, 8, 0)          # 오늘 지났으면 내일
    assert nf({"kind": "weekdays", "at": "09:00"}, SAT) == datetime(2026, 11, 16, 9, 0)      # 토 → 월
    assert nf({"kind": "weekly", "at": "18:30", "days": [2]}, SAT) == datetime(2026, 11, 18, 18, 30)   # 수
    assert nf({"kind": "every", "minutes": 30}, SAT) == datetime(2026, 11, 14, 9, 0)
    assert nf({"kind": "once", "date": "2026-11-20", "at": "07:05"}, SAT) == datetime(2026, 11, 20, 7, 5)
    assert nf({"kind": "once", "date": "2026-11-01", "at": "07:05"}, SAT) is None             # 지난 시각
    d = schedules.describe
    assert d({"kind": "weekly", "at": "18:30", "days": [0, 2]}) == "매주 월·수 18:30"
    assert d({"kind": "weekdays", "at": "09:00"}) == "평일 09:00" and d({"kind": "every", "minutes": 90}) == "90분마다"
    assert d({"kind": "once", "date": "2026-11-20", "at": "07:05"}) == "11-20 07:05 한 번"
    for bad in ({"kind": "daily", "at": "25:00"}, {"kind": "every", "minutes": 1}, {"kind": "weekly", "at": "09:00", "days": []},
                {"kind": "once", "at": "09:00", "date": "내일"}, {"kind": "nope"}):
        with pytest.raises(ValueError):
            schedules.When.parse(bad)


def test_api_create_list_toggle_delete_and_cli_refused(env):  # noqa: F811
    c = client()
    body = {"task": "어제 로그에서 오류만 모아 줘", "when": {"kind": "daily", "at": "09:00"},
            "options": {"profile": "general", "auto_approve": "writes", "max_steps": 20}}
    made = c.post("/api/v1/schedules", json=body, headers=H)
    assert made.status_code == 200, made.text
    s = made.json()
    assert s["title"] == "어제 로그에서 오류만 모아 줘" and s["when_text"] == "매일 09:00" and s["enabled"] and s["next_run_at"]
    assert [x["id"] for x in c.get("/api/v1/schedules").json()] == [s["id"]]
    off = c.patch(f"/api/v1/schedules/{s['id']}", json={"enabled": False}, headers=H).json()
    assert not off["enabled"] and off["next_run_at"] is None
    on = c.patch(f"/api/v1/schedules/{s['id']}", json={"enabled": True, "when": {"kind": "every", "minutes": 60}}, headers=H).json()
    assert on["enabled"] and on["when_text"] == "60분마다" and on["next_run_at"]
    assert c.post("/api/v1/schedules", json={**body, "options": {"model": "cc:sonnet"}}, headers=H).status_code == 400
    assert c.post("/api/v1/schedules", json={**body, "task": "  "}, headers=H).status_code == 400
    assert c.post("/api/v1/schedules", json={**body, "when": {"kind": "daily", "at": "9시"}}, headers=H).status_code == 400
    assert c.delete(f"/api/v1/schedules/{s['id']}", headers=H).status_code == 200
    assert c.get("/api/v1/schedules").json() == []


def test_run_now_starts_a_run_and_records_it(env):  # noqa: F811
    script, _ = env
    script.append(reply(content="모았습니다"))
    c = client()
    sid = c.post("/api/v1/schedules", json={"task": "요약해 줘", "when": {"kind": "daily", "at": "09:00"}}, headers=H).json()["id"]
    r = c.post(f"/api/v1/schedules/{sid}/run", headers=H).json()
    assert r["last_status"] == "시작함" and r["last_run_id"]
    run = wait_done(c, r["last_run_id"])
    assert run["status"] == "done" and run["task"] == "요약해 줘"


def test_tick_fires_due_skips_missed_and_finishes_once(env):  # noqa: F811
    script, _ = env
    script += [reply(content="끝"), reply(content="끝")]
    c = client()
    daily = c.post("/api/v1/schedules", json={"task": "매일 일", "when": {"kind": "daily", "at": "09:00"}}, headers=H).json()
    store = sched_api.store()
    due = datetime.fromisoformat(daily["next_run_at"])
    fired = sched_api.tick(due + timedelta(seconds=20))           # 제시간 (조금 늦어도) → 실행
    assert fired == [daily["id"]]
    after = store.get_schedule(daily["id"])
    assert after["last_status"] == "시작함" and datetime.fromisoformat(after["next_run_at"]) > due
    wait_done(c, after["last_run_id"])
    assert sched_api.tick(due + timedelta(seconds=30)) == []      # 같은 시각에 두 번 돌지 않는다
    # 서버가 꺼져 있어 한참 놓친 시각은 실행하지 않고 다음으로 넘긴다
    store.update_schedule(daily["id"], next_run_at=(due - timedelta(days=2)).isoformat(timespec="seconds"))
    assert sched_api.tick(due) == []
    skipped = store.get_schedule(daily["id"])
    assert skipped["last_status"].startswith("건너뜀") and datetime.fromisoformat(skipped["next_run_at"]) > due
    # 한 번만: 실행한 뒤 꺼진다 (매일 예약은 지워 같은 시각에 겹치지 않게)
    c.delete(f"/api/v1/schedules/{daily['id']}", headers=H)
    when = (due + timedelta(days=1))
    once = c.post("/api/v1/schedules", json={"task": "한 번 일", "when": {"kind": "once", "date": when.strftime("%Y-%m-%d"),
                                                                         "at": when.strftime("%H:%M")}}, headers=H).json()
    assert sched_api.tick(when + timedelta(seconds=5)) == [once["id"]]
    done = store.get_schedule(once["id"])
    assert not done["enabled"] and done["next_run_at"] is None
    wait_done(c, done["last_run_id"])


def test_busy_owner_is_skipped_not_queued(env, monkeypatch):  # noqa: F811
    from server import deps
    c = client()
    sid = c.post("/api/v1/schedules", json={"task": "일", "when": {"kind": "daily", "at": "09:00"}}, headers=H).json()["id"]
    monkeypatch.setattr(deps, "active", lambda owner=None: object())   # 그 사람이 이미 실행 중
    r = c.post(f"/api/v1/schedules/{sid}/run", headers=H).json()
    assert r["last_status"].startswith("건너뜀") and "실행 중" in r["last_status"] and not r["last_run_id"]


def test_each_user_sees_only_their_schedules(env, admin):  # noqa: F811
    a = issue(admin, "user_a")
    b = issue(admin, "user_b")
    sid = a.post("/api/v1/schedules", json={"task": "A 의 일", "when": {"kind": "daily", "at": "09:00"}}, headers=H).json()["id"]
    assert b.get("/api/v1/schedules").json() == []
    for call in (lambda: b.patch(f"/api/v1/schedules/{sid}", json={"enabled": False}, headers=H),
                 lambda: b.delete(f"/api/v1/schedules/{sid}", headers=H), lambda: b.post(f"/api/v1/schedules/{sid}/run", headers=H)):
        assert call().status_code == 404
    mine = a.get("/api/v1/schedules").json()
    assert [x["id"] for x in mine] == [sid] and mine[0]["owner"] == "user_a"
    assert sid in [x["id"] for x in admin.get("/api/v1/schedules").json()]   # 관리자는 모두 본다
