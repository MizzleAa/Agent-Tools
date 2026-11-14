"""Claude Code 기능의 오류. API 에서는 {"detail": message} + X-Error-Code 머리글이 된다 (SDK 없이도 불러올 수 있게 따로 둔다)."""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code, self.code, self.message, self.extra = status_code, code, message, extra or {}
