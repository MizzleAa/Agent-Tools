"""개발 규격 읽기 도구. 언어를 고른 뒤 그 언어 규격을 읽는다 (agent/standards.py)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from agent import standards
from agent.tools.base import ToolContext, tool


@tool("read")
def read_standard(
    ctx: ToolContext,
    name: Annotated[Literal["common", "python", "dotnet", "node-web"],
                    Field(description="common=공통 규격 전체, python, dotnet(C#), node-web(JavaScript/Node·웹)")],
) -> str:
    """개발 규격을 읽는다. 스택을 고른 뒤 그 언어 규격(프로젝트 구성·의존성·명령·검증·정리)을 읽고 따른다."""
    ctx.memo.setdefault("standards_read", set()).add(name)
    return standards.load(name)
