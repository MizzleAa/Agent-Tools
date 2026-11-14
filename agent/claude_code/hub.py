"""화면(SSE)으로 보내는 이벤트 허브.

전역 이벤트(세션·프로세스 상태 변화)는 모든 화면에, 세션 이벤트(스트리밍·권한 요청)는
그 세션을 구독한 화면에만 보낸다.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

GLOBAL_TYPES = {"session.updated", "session.deleted", "project.updated", "project.deleted", "process.updated", "rate_limit"}
MAX_QUEUE = 20_000


@dataclass
class Client:
    id: int
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE))
    subscriptions: set[str] = field(default_factory=set)
    dropped: bool = False


class EventHub:
    def __init__(self) -> None:
        self._clients: dict[int, Client] = {}
        self._ids = itertools.count(1)
        self._seq = itertools.count(1)
        self.last_client_seen = time.time()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def add_client(self) -> Client:
        c = Client(next(self._ids))
        self._clients[c.id] = c
        self.last_client_seen = time.time()
        return c

    def remove_client(self, c: Client) -> None:
        self._clients.pop(c.id, None)
        self.last_client_seen = time.time()

    def envelope(self, type_: str, session_id: str | None, data: dict[str, Any]) -> dict[str, Any]:
        return {"type": type_, "session_id": session_id, "seq": next(self._seq), "ts": time.time(), "data": data}

    def send_to(self, c: Client, type_: str, session_id: str | None, data: dict[str, Any]) -> None:
        self._put(c, self.envelope(type_, session_id, data))

    def publish(self, type_: str, session_id: str | None, data: dict[str, Any]) -> None:
        ev = self.envelope(type_, session_id, data)
        is_global = type_ in GLOBAL_TYPES
        for c in list(self._clients.values()):
            if is_global or (session_id and session_id in c.subscriptions):
                self._put(c, ev)

    def _put(self, c: Client, ev: dict[str, Any]) -> None:
        if c.dropped:
            return
        try:
            c.queue.put_nowait(ev)
        except asyncio.QueueFull:
            # 너무 느린 화면은 끊는다 — 다시 연결하면 스냅숏으로 따라잡는다.
            log.warning("client %s queue full; dropping", c.id)
            c.dropped = True
            try:
                c.queue.get_nowait()
                c.queue.put_nowait({"type": "resync", "session_id": None, "seq": next(self._seq), "ts": time.time(), "data": {}})
            except Exception:  # noqa: BLE001
                pass
