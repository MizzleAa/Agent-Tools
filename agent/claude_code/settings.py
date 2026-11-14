"""Claude Code 탭 설정 기본값과 검증. 값은 SQLite settings 표에 key → JSON 으로 저장한다."""
from __future__ import annotations

from typing import Any

from .store import Database

# 권한 우회(bypassPermissions)·dontAsk 는 GUI 에서 고를 수 없다 — 승인 절차는 끄지 않는다.
ALLOWED_PERMISSION_MODES = ("default", "acceptEdits", "plan")

DEFAULTS: dict[str, Any] = {
    "cli_path": "",                     # 비우면 자동 탐색
    "codex_path": "",                   # Codex CLI (codex.exe 또는 codex.js) — 비우면 자동 탐색 (engines.locate)
    "gemini_path": "",                  # Gemini CLI (gemini.js) — 비우면 자동 탐색
    "default_model": "",                # 비우면 CLI 기본 모델
    "default_permission_mode": "default",
    "prewarm_process": True,            # 세션을 만들거나 열 때 CLI 를 미리 띄운다
    "idle_process_timeout_min": 30,     # 작업 없는 CLI 프로세스를 닫는 시간 (세션 기록은 그대로)
    "max_processes": 6,                 # 동시에 살아 있는 CLI 프로세스 수 상한
    "show_thinking": True,
    "show_external_sessions": True,     # 다른 곳(터미널·VS Code)에서 만든 Claude Code 세션도 목록에 보이기
    "send_with_enter": True,            # Enter 전송 (False 면 Ctrl+Enter)
}


def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def normalize(key: str, value: Any) -> Any:
    if key not in DEFAULTS:
        raise KeyError(key)
    d = DEFAULTS[key]
    if key == "default_permission_mode":
        return value if value in ALLOWED_PERMISSION_MODES else "default"
    if key == "idle_process_timeout_min":
        return _clamp_int(value, 1, 24 * 60, d)
    if key == "max_processes":
        return _clamp_int(value, 1, 32, d)
    if isinstance(d, bool):
        return bool(value)
    if isinstance(d, str):
        return "" if value is None else str(value).strip()
    return value


class Settings:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> dict[str, Any]:
        stored = self.db.get_settings()
        out = dict(DEFAULTS)
        for k, v in stored.items():
            if k in DEFAULTS:
                out[k] = normalize(k, v)
        return out

    def get(self, key: str) -> Any:
        return self.all()[key]

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        for k, v in values.items():
            if k in DEFAULTS and v is not None:
                self.db.set_setting(k, normalize(k, v))
        return self.all()
