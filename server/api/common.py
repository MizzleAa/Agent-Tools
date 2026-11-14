"""여러 라우터가 같이 쓰는 응답 모양과 오류 처리.

오류 응답은 항상 `{"detail": "사람이 읽을 한국어 설명"}` 하나다 (입력 검증 오류도 400 + 같은 모양).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError


class ApiModel(BaseModel):
    """API 모양의 바탕. 응답 모양에서는 기본값이 있는 필드도 '항상 온다(required)'로 문서화한다
    (요청 모양에서는 생략할 수 있다). 그래야 생성되는 TypeScript 타입이 정확하다."""
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Ok(ApiModel):
    ok: bool = True


class ErrorResponse(ApiModel):
    detail: str


# 라우터에 붙이는 공통 오류 설명 (OpenAPI 문서·생성되는 TS 타입에 나타난다)
ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "입력이 잘못됨"},
    403: {"model": ErrorResponse, "description": "토큰이 없거나 허용되지 않은 Host"},
    404: {"model": ErrorResponse, "description": "대상이 없음"},
    409: {"model": ErrorResponse, "description": "지금은 할 수 없음 (실행 중 등)"},
}


def validation_text(e: ValidationError | list[dict[str, Any]]) -> str:
    errors = e.errors() if isinstance(e, ValidationError) else e
    return "; ".join(f"{'.'.join(str(x) for x in err.get('loc', ()) if x != 'body') or '값'}: {err.get('msg', '')}"
                     for err in errors)


def bad_request(e: Exception) -> HTTPException:
    """ValueError·ValidationError → 400 (메시지는 그대로 화면에 보인다)."""
    if isinstance(e, ValidationError):
        return HTTPException(400, validation_text(e))
    return HTTPException(400, str(e))
