"""Claude 회사 계정 로그인을 관리 화면에서 — 서버가 Anthropic 공식 CLI(`ant auth login --profile <이름>`)를 대신 띄운다.

- 서버 PC 에서 누르면: 공식 CLI 가 서버 PC 에 브라우저를 열고, 로그인 · 조직/워크스페이스 선택이 끝나면 스스로 끝난다.
- 다른 PC 에서 누르면: `--no-browser` 로 띄워, CLI 가 내놓는 로그인 주소를 화면에 보여 주고 사용자가 붙여 넣은 코드를 CLI 에 넘긴다.
- 끝나면(성공) 그 연결의 모델 목록을 다시 받는다. 토큰은 공식 CLI·SDK 가 %APPDATA%/Anthropic 에 저장 · 갱신한다
  (이 프로그램은 비밀번호도 토큰도 보지 않는다. 화면에 보이는 출력에도 토큰은 없다 — 혹시 섞이면 가린다).
- 한 연결에 로그인 하나씩. 10분 안에 끝나지 않으면 끊는다.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from agent import config

TIMEOUT = 600
_URL = re.compile(r"https?://[^\s'\"<>]+")
_SECRETISH = re.compile(r"(sk-ant-[\w-]+|[A-Za-z0-9_-]{60,})")   # 혹시 출력에 토큰 같은 긴 값이 섞이면 가린다


@dataclass
class Session:
    provider_id: str
    mode: str                        # browser (서버 PC 에 브라우저) · manual (주소를 보여 주고 코드를 받는다)
    proc: Any = None
    lines: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    state: str = "running"           # running · done · failed · cancelled
    message: str = ""
    started: float = field(default_factory=time.time)
    exit_code: int | None = None

    def view(self) -> dict[str, Any]:
        return {"provider_id": self.provider_id, "mode": self.mode, "state": self.state, "message": self.message,
                "urls": list(self.urls), "output": self.lines[-30:], "seconds": round(time.time() - self.started)}


_lock = threading.Lock()
_sessions: dict[str, Session] = {}


def argv(profile: str, manual: bool) -> list[str]:
    """띄울 명령 (테스트는 가짜 CLI 로 바꿔 끼운다)."""
    from agent.providers import ANT_EXE
    return [str(config.ROOT / ANT_EXE), "auth", "login", "--profile", profile, *(["--no-browser"] if manual else [])]


def tool_ready() -> bool:
    from agent.providers import ANT_EXE
    return (config.ROOT / ANT_EXE).is_file()


def status(provider_id: str) -> dict[str, Any] | None:
    with _lock:
        s = _sessions.get(provider_id)
        return s.view() if s else None


def start(provider_id: str, profile: str, manual: bool, on_done: Any = None) -> dict[str, Any]:
    """로그인을 시작한다 (이미 진행 중이면 그것을 돌려준다). on_done: 성공하면 부를 함수 (모델 목록 다시 받기)."""
    with _lock:
        cur = _sessions.get(provider_id)
        if cur and cur.state == "running":
            return cur.view()
        cmd = argv(profile, manual)
        s = Session(provider_id, "manual" if manual else "browser")
        try:
            s.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8", errors="replace", bufsize=1,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                      env={k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")})
        except OSError as e:
            s.state, s.message = "failed", f"로그인 도구를 띄우지 못했습니다: {e}"
            _sessions[provider_id] = s
            return s.view()
        s.message = ("서버 PC 화면에 브라우저가 열립니다. 회사 Claude 계정으로 로그인하고 조직 · 워크스페이스를 고르세요."
                     if not manual else "아래 주소를 열어 회사 Claude 계정으로 로그인한 뒤, 받은 코드를 붙여 넣으세요.")
        _sessions[provider_id] = s
    threading.Thread(target=_watch, args=(s, on_done), daemon=True).start()
    return s.view()


def _watch(s: Session, on_done: Any) -> None:
    def read() -> None:
        for raw in s.proc.stdout:
            line = _SECRETISH.sub("***", raw.rstrip())
            if not line:
                continue
            with _lock:
                s.lines.append(line)
                for url in _URL.findall(raw):
                    if url not in s.urls:
                        s.urls.append(url)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        code = s.proc.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        s.proc.kill()
        code = None
    reader.join(timeout=2)
    with _lock:
        if s.state == "cancelled":
            return
        s.exit_code = code
        if code == 0:
            s.state, s.message = "done", "로그인했습니다. 모델 목록을 받는 중…"
        else:
            s.state = "failed"
            s.message = "시간이 지나 로그인을 끊었습니다 (10분)." if code is None else \
                f"로그인이 끝나지 않았습니다 (종료 코드 {code}). 아래 출력을 확인하고 다시 시도하세요."
    if code == 0 and on_done is not None:
        try:
            on_done()
            with _lock:
                s.message = "로그인했습니다. 연결을 확인했습니다."
        except Exception as e:   # noqa: BLE001
            with _lock:
                s.message = f"로그인은 됐지만 연결 확인에 실패했습니다: {e}"


def send(provider_id: str, text: str) -> dict[str, Any]:
    """다른 PC 에서 로그인할 때: 브라우저에서 받은 코드를 로그인 도구에 넘긴다 (기록에 남기지 않는다)."""
    with _lock:
        s = _sessions.get(provider_id)
        if s is None or s.state != "running" or s.proc is None or s.proc.stdin is None:
            raise ValueError("진행 중인 로그인이 없습니다. '로그인'을 다시 누르세요.")
        try:
            s.proc.stdin.write(text.strip() + "\n")
            s.proc.stdin.flush()
        except OSError as e:
            raise ValueError(f"로그인 도구에 코드를 넘기지 못했습니다: {e}")
        s.lines.append("(코드를 넘겼습니다)")
        return s.view()


def cancel(provider_id: str) -> None:
    with _lock:
        s = _sessions.get(provider_id)
        if s is None or s.state != "running":
            return
        s.state, s.message = "cancelled", "로그인을 그만뒀습니다."
        proc = s.proc
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass
