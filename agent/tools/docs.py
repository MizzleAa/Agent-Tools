"""문서 Q&A 도구 (7단계): 폴더의 문서를 색인하고, 질문과 관련된 부분을 낱말 + 의미로 찾는다 (agent/doclib.py)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from agent import doclib, embedding
from agent.index import DocIndex
from agent.tools.base import ToolContext, ToolError, tool

NO_VECTOR = "임베딩 모델(models/Qwen3-Embedding…gguf)이 없거나 꺼져 있습니다"


def _index(ctx: ToolContext) -> DocIndex:
    return DocIndex(ctx.settings.data_path / "index.db")


@tool("read", taints=True)
def index_documents(
    ctx: ToolContext,
    path: Annotated[str, Field(description="문서가 든 폴더 (작업 폴더 기준 또는 허용된 위치). 안의 폴더까지 읽는다")],
) -> str:
    """폴더 안의 문서(PDF · 워드 · 엑셀 · CSV · MD · TXT …)를 읽어 문서 검색 색인에 넣는다. 바뀐 파일만 다시 읽으니 다시 불러도 된다.
    색인한 뒤에는 search_documents 로 찾는다."""
    folder = ctx.sandbox.resolve(path)
    if not folder.is_dir():
        raise ToolError(f"폴더가 아닙니다 (없거나 파일입니다): {path}")
    emb = embedding.get_embedder(ctx.settings)
    index = _index(ctx)
    try:
        r = doclib.index_folder(index, folder, emb)
    finally:
        index.close()
    lines = [f"색인했습니다: {ctx.rel(folder)} — 문서 {r['files']}개 (새로 {r['added']} · 바뀜 {r['updated']} · "
             f"그대로 {r['unchanged']} · 지움 {r['removed']})" + (" — 파일이 많아 앞부분만" if r["truncated"] else "")]
    if r["failed"]:
        lines.append(f"읽지 못한 파일 {len(r['failed'])}개: " + "; ".join(r["failed"][:5]) + (" …" if len(r["failed"]) > 5 else ""))
    if r["vector"]:
        lines.append(f"의미 검색 준비됨 (이번에 임베딩한 조각 {r['embedded']}개)")
    else:
        lines.append(f"의미 검색 없이 낱말 검색만 — {r['vector_error'] or NO_VECTOR}")
    lines.append("이제 search_documents 로 찾으세요.")
    return "\n".join(lines)


@tool("read", taints=True)
def search_documents(
    ctx: ToolContext,
    query: Annotated[str, Field(description="찾을 내용 (질문 그대로 써도 된다). 예) 노즐 교체 주기")],
    folder: Annotated[str, Field(description="색인한 폴더 하나로 좁힐 때. 비우면 색인한 문서 전체")] = "",
) -> str:
    """색인한 문서에서 질문과 관련된 부분을 찾는다 (낱말 + 의미 — 같은 뜻의 다른 말도 찾는다).
    결과의 파일 경로를 근거로 답하고, 더 자세히는 read_file 로 그 파일을 읽는다."""
    source = doclib.source_for(ctx.sandbox.resolve(folder)) if folder.strip() else None
    path = ctx.settings.data_path / "index.db"
    index = _index(ctx) if path.exists() else None
    try:
        if index is None or not index.count_docs(doclib.SOURCE_PREFIX, source=source):
            return ("아직 색인한 문서가 없습니다" + (f" ({folder})" if folder.strip() else "")
                    + " — 먼저 index_documents 로 문서 폴더를 색인하세요.")
        emb = embedding.get_embedder(ctx.settings)
        qvec, note = None, ""
        if emb is None:
            note = f"(의미 검색 없이 낱말 검색만 — {NO_VECTOR})"
        else:
            try:
                qvec = emb.embed_query(query)
            except embedding.EmbeddingError as e:
                note = f"(의미 검색 없이 낱말 검색만 — {e})"
        hits = index.hybrid_search(query, qvec, doclib.SOURCE_PREFIX, source=source, limit=6,
                                   max_distance=ctx.settings.embedding.max_distance, snippet_chars=doclib.SNIPPET_CHARS)
    finally:
        if index is not None:
            index.close()
    if not hits:
        return "찾은 부분이 없습니다. 다른 말(동의어 · 약어 · 영어 표기)로 다시 찾아보세요." + (f"\n{note}" if note else "")
    body = "\n".join(f"{i}. {h['title']} — {h['url']}\n   ({h['how']}) {h['snippet']}" for i, h in enumerate(hits, 1))
    return body + (f"\n{note}" if note else "")
