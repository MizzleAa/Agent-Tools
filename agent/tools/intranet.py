"""사내 페이지 검색 도구. 미리 수집해 둔 색인(data/index.db)에서 찾는다 (agent/crawler.py 가 채움)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from agent.index import DocIndex
from agent.tools.base import ToolContext, ToolError, tool


@tool("read", taints=True)
def search_intranet(
    ctx: ToolContext,
    query: Annotated[str, Field(description="찾을 단어들 (띄어쓰기로 구분). 예) 설비 점검 주기")],
    source: Annotated[str, Field(description="특정 사이트 이름으로 좁힐 때. 비우면 전체")] = "",
) -> str:
    """수집해 둔 사내 웹페이지에서 검색해 제목·주소·관련 부분을 돌려준다. 자세히 보려면 fetch_url 로 연다."""
    path = ctx.settings.data_path / "index.db"
    if not path.exists():
        raise ToolError("사내 색인이 비어 있습니다. 관리 화면 '상태' 탭에서 '지금 수집'을 먼저 실행하세요")
    index = DocIndex(path)
    try:
        if index.count() == 0:
            raise ToolError("사내 색인이 비어 있습니다. 관리 화면 '상태' 탭에서 '지금 수집'을 먼저 실행하세요")
        hits = index.search(query, source=source or None, limit=8)
    finally:
        index.close()
    if not hits:
        return "찾은 결과가 없습니다. 다른 단어(동의어, 약어, 영어 표기)로 다시 검색해 보세요."
    return "\n".join(f"{i}. [{h['source']}] {h['title']}\n   {h['url']}\n   {h['snippet']}"
                     for i, h in enumerate(hits, 1))
