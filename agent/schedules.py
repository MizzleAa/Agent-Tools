"""9단계 예약 실행 — 시각 규칙(When)과 다음 실행 시각.

저장은 실행 기록 DB 의 schedules 표(agent/store.py), 실행은 server/api/schedules.py 가 30초마다 tick(now) 로 한다
(APScheduler 대신 단순한 반복 — 다음 실행 시각을 DB 에 두므로 관리 서버를 다시 켜도 이어지고, 테스트는 시간을 기다리지 않는다).

    once      정한 날짜 · 시각에 한 번 (실행하면 꺼진다)
    daily     매일 그 시각          weekdays  월~금 그 시각
    weekly    고른 요일(0=월 … 6=일) 그 시각
    every     N분마다 (5분 이상)
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

DAYS = "월화수목금토일"
MIN_EVERY = 5
GRACE = timedelta(minutes=15)   # 관리 서버가 꺼져 있다 켜졌을 때, 이보다 오래 놓친 시각은 실행하지 않고 다음으로 넘긴다


class When(BaseModel):
    kind: Literal["once", "daily", "weekdays", "weekly", "every"]
    at: str = "09:00"
    date: str = ""
    days: list[int] = Field(default_factory=list)
    minutes: int = 60

    @model_validator(mode="after")
    def _check(self) -> "When":
        if self.kind != "every" and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", self.at):
            raise ValueError(f"시각은 00:00 ~ 23:59 로 적으세요: {self.at}")
        if self.kind == "once":
            try:
                date.fromisoformat(self.date)
            except ValueError:
                raise ValueError(f"날짜는 2026-11-20 처럼 적으세요: {self.date}") from None
        if self.kind == "weekly":
            if not self.days or any(d not in range(7) for d in self.days):
                raise ValueError("매주 실행할 요일을 하나 이상 고르세요 (0=월 … 6=일)")
            self.days = sorted(set(self.days))
        if self.kind == "every" and not MIN_EVERY <= self.minutes <= 7 * 24 * 60:
            raise ValueError(f"간격은 {MIN_EVERY}분 이상으로 정하세요")
        return self

    @classmethod
    def parse(cls, raw: Any) -> "When":
        try:
            return raw if isinstance(raw, When) else cls.model_validate(raw)
        except ValidationError as e:
            msgs = [str(x.get("ctx", {}).get("error") or x.get("msg")) for x in e.errors()]
            raise ValueError("; ".join(m.removeprefix("Value error, ") for m in msgs)) from None


def next_fire(when: Any, now: datetime) -> datetime | None:
    """now 보다 뒤의 다음 실행 시각 (없으면 None — 지난 '한 번')."""
    w = When.parse(when)
    now = now.replace(microsecond=0)
    if w.kind == "every":
        return now + timedelta(minutes=w.minutes)
    hh, mm = map(int, w.at.split(":"))
    if w.kind == "once":
        t = datetime.combine(date.fromisoformat(w.date), time(hh, mm))
        return t if t > now else None
    for add in range(8):
        d = now.date() + timedelta(days=add)
        t = datetime.combine(d, time(hh, mm))
        if t > now and (w.kind == "daily" or (w.kind == "weekdays" and d.weekday() < 5)
                        or (w.kind == "weekly" and d.weekday() in w.days)):
            return t
    return None


def describe(when: Any) -> str:
    w = When.parse(when)
    if w.kind == "once":
        return f"{w.date[5:]} {w.at} 한 번"
    if w.kind == "daily":
        return f"매일 {w.at}"
    if w.kind == "weekdays":
        return f"평일 {w.at}"
    if w.kind == "weekly":
        return "매주 " + "·".join(DAYS[d] for d in w.days) + f" {w.at}"
    return f"{w.minutes}분마다"
