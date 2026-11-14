"""하네스 자료(관리자가 올린 사용설명서 · 에러 대처 같은 QA 자료 — agent/harness.py) 찾기 · 읽기.

자료 본문은 시스템 지침에 넣지 않는다 (16k 모델이 넘친다). 지침에는 목록만 있고, 필요한 부분을 이 도구로 꺼내 읽는다.
로그인 사용 모드의 일반 사용자는 자기 회사가 대상인 폴더의 자료만 찾고 읽는다 (Settings.audience).
자료가 하나라도 있으면 모든 용도에 자동으로 붙는다 (agent/loop.py).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from agent import harness
from agent.tools.base import ToolContext, ToolError, tool


@tool("read")
def search_knowledge(
    ctx: ToolContext,
    query: Annotated[str, Field(description="찾을 말 (띄어쓰기로 구분). 예) E-104 과열, 노즐 막힘 청소, 초기 설정")],
    folder: Annotated[str, Field(description="특정 자료 폴더로 좁힐 때 (예: A사/장비-X100, 다른 하네스면 @이름/폴더). 비우면 전체")] = "",
) -> str:
    """하네스 자료(사용설명서 · 에러 대처 · 점검 절차 같은 QA 자료)에서 관련 부분을 찾는다. 장비 오류·사용법·절차 질문이면 먼저 쓴다."""
    hits = harness.search(ctx.settings.audience, query, folder, scopes=harness.active(ctx.settings.audience, ctx.settings.harness))
    if not hits:
        return (f"'{query}' 에 맞는 하네스 자료가 없습니다. 다른 낱말(에러 코드, 부품 이름, 영어 표기)로 다시 찾거나, "
                "자료에 없으면 없다고 답하세요 (짐작해서 쓰지 않는다).")
    lines = [f"찾은 자료 {len(hits)}곳 (자세히 보려면 read_knowledge(path)):"]
    for h in hits:
        where = f"{h['path']}" + (f" › {h['section']}" if h["section"] else "")
        lines.append(f"- {where} — {h['title']}\n  {h['snippet']}")
    return "\n".join(lines)


@tool("read")
def read_knowledge(
    ctx: ToolContext,
    path: Annotated[str, Field(description="자료 경로 (search_knowledge 결과나 지침의 자료 목록에 있는 그대로)")],
    start: Annotated[int, Field(description="긴 자료를 이어 읽을 때 시작 위치 (앞 결과에 나온 값)", ge=0)] = 0,
) -> str:
    """하네스 자료 하나를 읽는다. 답에는 읽은 자료 경로를 근거로 적는다."""
    try:
        text, nxt, total = harness.read(ctx.settings.audience, path, start, max_chars=min(6000, ctx.settings.limits.tool_output_chars + 2000),
                                        scopes=harness.active(ctx.settings.audience, ctx.settings.harness))
    except ValueError as e:
        raise ToolError(str(e))
    tail = f"\n\n…(이어서 읽으려면 read_knowledge(path='{path}', start={nxt}) — 전체 {total}자)" if nxt >= 0 else ""
    return f"[하네스 자료] {path}\n\n{text}{tail}"
