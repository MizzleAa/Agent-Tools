"""도구 레지스트리.

평범한 파이썬 함수에 @tool 을 붙이면:
  1) 함수 시그니처에서 JSON 스키마를 만들어 모델에게 보여주고 (Tool.schema)
  2) 모델이 보낸 인자를 pydantic 으로 검증한 뒤 함수를 호출한다 (Tool.call)

첫 번째 인자 `ctx` 는 스키마에서 빠지고, 하네스가 ToolContext 를 넣어준다.
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, get_type_hints

from pydantic import BaseModel, create_model

from agent.config import Settings
from agent.safety import Sandbox

# send = 사내 시스템 데이터를 바꾸는 API 호출, admin = 에이전트 자신의 접근 범위(설정)를 바꾸는 일
Permission = Literal["read", "write", "exec", "send", "admin"]


class ToolError(Exception):
    """도구가 모델에게 돌려줄 '예상된' 실패 (파일 없음 등)."""


class Failed(str):
    """출력은 그대로 돌려주되 실행 기록에는 실패로 남길 결과 (HTTP 403·404, 대상이 없는 빈 응답 — QA S-9).
    예외로 바꾸면 응답 본문(거절 이유)이 사라지므로 글자는 유지하고 표시만 붙인다."""
    failed = True


@dataclass
class ToolContext:
    settings: Settings
    sandbox: Sandbox
    run_dir: Path
    cancel: threading.Event = field(default_factory=threading.Event)
    memo: dict[str, Any] = field(default_factory=dict)   # 한 실행 동안 도구들이 기억할 것 (예: 처음부터 읽은 파일)

    def rel(self, p: Path) -> str:
        """작업 폴더 안이면 상대 경로로, 아니면 절대 경로로 보여준다 (모델 토큰 절약)."""
        try:
            return str(p.relative_to(self.sandbox.cwd)) or "."
        except ValueError:
            return str(p)


@dataclass
class Tool:
    name: str
    description: str
    permission: Permission
    fn: Callable[..., str]
    params: type[BaseModel]
    taints: bool = False  # 외부 내용(웹/문서)을 가져오는 도구면 True

    def schema(self) -> dict[str, Any]:
        params = self.params.model_json_schema()
        _strip_titles(params)
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": params}}

    def call(self, ctx: ToolContext, arguments: dict[str, Any]) -> str:
        args = self.params.model_validate(arguments)  # 잘못된 인자면 ValidationError
        return self.fn(ctx, **args.model_dump())


REGISTRY: dict[str, Tool] = {}


def tool(permission: Permission = "read", taints: bool = False):
    def deco(fn: Callable[..., str]) -> Callable[..., str]:
        hints = get_type_hints(fn, include_extras=True)
        fields: dict[str, Any] = {}
        for name, p in inspect.signature(fn).parameters.items():
            if name == "ctx":
                continue
            default = ... if p.default is inspect.Parameter.empty else p.default
            fields[name] = (hints[name], default)
        REGISTRY[fn.__name__] = Tool(
            name=fn.__name__,
            description=inspect.getdoc(fn) or "",
            permission=permission,
            fn=fn,
            params=create_model(f"{fn.__name__}_args", **fields),
            taints=taints,
        )
        return fn

    return deco


def decode_bytes(data: bytes) -> str:
    """UTF-8 로 읽고, 안 되면 한국어 Windows 기본 인코딩(cp949)으로 읽는다."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


def _strip_titles(node: Any) -> None:
    """pydantic 이 붙이는 'title' 을 지운다. 소형 모델에게는 스키마가 짧을수록 좋다."""
    if isinstance(node, dict):
        if isinstance(node.get("title"), str):
            node.pop("title")
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for prop in value.values():
                    _strip_titles(prop)
            else:
                _strip_titles(value)
    elif isinstance(node, list):
        for value in node:
            _strip_titles(value)
