"""7단계 문서 Q&A: 폴더의 문서를 색인(FTS5 낱말 + sqlite-vec 의미)해 근거와 함께 찾는다.

진짜 임베딩 모델(Qwen3-Embedding GGUF · llama-server --embedding) 없이 돈다 — FakeEmbedder 가 '같은 뜻 낱말 묶음'으로
벡터를 만들어, 낱말이 하나도 겹치지 않아도 뜻이 같으면 찾히는지(의미 검색)를 시험한다.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import httpx
import pytest

from agent import doclib, embedding
from agent.index import DocIndex
from agent.profiles import load_profile
from agent.tools import REGISTRY, ToolError
from agent.tools.docs import index_documents, search_documents

GROUPS = [["노즐", "분사구", "nozzle"], ["교체", "갈아", "바꾸"], ["보증", "워런티", "warranty"], ["온도", "열", "과열"],
          ["압력", "pressure"], ["점심", "식당", "메뉴"]]


class FakeEmbedder:
    """같은 묶음의 낱말은 같은 축 — '노즐 교체' 와 '분사구 갈아끼우기' 가 가깝다."""
    dim = len(GROUPS) + 1

    def __init__(self):
        self.calls = 0

    def _vec(self, text: str) -> list[float]:
        v = [float(sum(text.count(w) for w in g)) for g in GROUPS] + [0.01]
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class BrokenEmbedder(FakeEmbedder):
    def embed_documents(self, texts):
        raise embedding.EmbeddingError("임베딩 서버가 응답하지 않습니다")

    def embed_query(self, text):
        raise embedding.EmbeddingError("임베딩 서버가 응답하지 않습니다")


@pytest.fixture
def docs(ws: Path) -> Path:
    d = ws / "매뉴얼"
    d.mkdir()
    (d / "x100.md").write_text("# X100\n## 소모품\n분사구는 800시간마다 갈아끼운다.\n\n## 보증\n본체 보증은 24개월이다.\n", encoding="utf-8")
    (d / "식당.txt").write_text("오늘 점심 메뉴는 비빔밥이다. 식당은 2층.", encoding="utf-8")
    (d / "그림.png").write_bytes(b"\x89PNG\r\n")   # 문서가 아닌 것은 건너뛴다
    return d


def _index(settings) -> DocIndex:
    return DocIndex(settings.data_path / "index.db")


def test_index_folder_adds_text_and_vectors_then_skips_unchanged(settings, docs):
    idx, emb = _index(settings), FakeEmbedder()
    try:
        assert idx.has_vec
        first = doclib.index_folder(idx, docs, emb)
        assert (first["files"], first["added"], first["unchanged"], first["failed"]) == (2, 2, 0, [])
        assert first["embedded"] >= 2 and first["vector"]
        again = doclib.index_folder(idx, docs, emb)
        assert (again["added"], again["updated"], again["unchanged"], again["embedded"]) == (0, 0, 2, 0)   # 그대로면 다시 임베딩하지 않는다
        (docs / "식당.txt").unlink()
        (docs / "x100.md").write_text("# X100\n## 보증\n본체 보증은 36개월로 늘었다.\n", encoding="utf-8")
        third = doclib.index_folder(idx, docs, emb)
        assert (third["updated"], third["removed"]) == (1, 1)
        assert idx.pending_chunks(doclib.source_for(docs)) == []   # 바뀐 조각은 새로 임베딩, 지운 문서의 벡터도 지운다
        hits = idx.hybrid_search("보증 기간", emb.embed_query("보증 기간"), doclib.SOURCE_PREFIX)
        assert hits and "36개월" in hits[0]["snippet"] and not any("점심" in h["snippet"] for h in hits)
    finally:
        idx.close()


def test_meaning_search_finds_passage_without_shared_words(settings, docs):
    """'노즐 교체' 는 문서에 그대로 없다 (문서는 '분사구 … 갈아끼운다') — 낱말 검색은 못 찾고 의미 검색이 찾는다."""
    idx, emb = _index(settings), FakeEmbedder()
    try:
        doclib.index_folder(idx, docs, emb)
        assert idx.search("노즐 교체", source=doclib.source_for(docs)) == []
        hits = idx.hybrid_search("노즐 교체", emb.embed_query("노즐 교체"), doclib.SOURCE_PREFIX)
        assert hits[0]["title"] == "x100.md" and "800시간" in hits[0]["snippet"] and hits[0]["how"] == "의미"
        both = idx.hybrid_search("보증 개월", emb.embed_query("보증 개월"), doclib.SOURCE_PREFIX)
        assert both[0]["how"] == "낱말+의미" and "24개월" in both[0]["snippet"]
        assert idx.hybrid_search("보증", None, doclib.SOURCE_PREFIX)[0]["how"] == "낱말"   # 벡터 없이 부르면 낱말만
    finally:
        idx.close()


def test_document_chunks_split_at_headings_but_keep_heading_only_lines_with_body():
    from agent.index import chunk_text
    text = "# X100\n## 소모품\n분사구는 800시간마다.\n## 보증\n24개월."
    assert chunk_text(text, 600, headings=True) == ["# X100\n## 소모품\n분사구는 800시간마다.", "## 보증\n24개월."]
    assert chunk_text(text, 600) == [text]   # 사내 페이지(기본)는 그대로


def test_meaning_hits_far_from_the_best_are_dropped(settings, docs):
    idx, emb = _index(settings), FakeEmbedder()
    try:
        doclib.index_folder(idx, docs, emb)
        q = emb.embed_query("노즐 교체")
        loose = idx.hybrid_search("노즐 교체", q, doclib.SOURCE_PREFIX)
        tight = idx.hybrid_search("노즐 교체", q, doclib.SOURCE_PREFIX, max_distance=0.7)
        assert any("점심" in h["snippet"] for h in loose)          # 상한이 없으면 관계없는 것도 뒤에 붙는다
        assert tight and not any("점심" in h["snippet"] for h in tight) and "800시간" in tight[0]["snippet"]
    finally:
        idx.close()


def test_rrf_merges_rankings():
    fused = doclib.rrf([[1, 2, 3], [3, 4]], k=60)
    assert fused[0] == 3 and set(fused) == {1, 2, 3, 4}   # 두 목록에 다 있는 것이 위로


def test_tools_index_then_search_with_sources(ctx, docs, monkeypatch):
    emb = FakeEmbedder()
    monkeypatch.setattr(embedding, "get_embedder", lambda settings: emb)
    out = index_documents(ctx, path="매뉴얼")
    assert "문서 2개" in out and "새로 2" in out and "의미 검색 준비됨" in out
    res = search_documents(ctx, query="노즐은 언제 교체하나요")
    assert "x100.md" in res and "800시간" in res and str(docs) in res and "의미" in res
    assert "점심" not in res.split("\n")[0]
    with pytest.raises(ToolError):
        index_documents(ctx, path="없는폴더")


def test_search_falls_back_to_words_when_embedding_is_unavailable(ctx, docs, monkeypatch):
    monkeypatch.setattr(embedding, "get_embedder", lambda settings: BrokenEmbedder())
    out = index_documents(ctx, path="매뉴얼")
    assert "문서 2개" in out and "낱말 검색만" in out          # 색인은 되고, 임베딩 실패는 알린다
    res = search_documents(ctx, query="보증")
    assert "24개월" in res and "낱말 검색만" in res
    monkeypatch.setattr(embedding, "get_embedder", lambda settings: None)   # 임베딩 모델 파일이 없는 PC
    assert "24개월" in search_documents(ctx, query="보증")
    assert "아직 색인한 문서가 없습니다" in search_documents(ctx, query="보증", folder="다른폴더")


def test_index_outside_allowed_area_is_refused(ctx, monkeypatch):
    monkeypatch.setattr(embedding, "get_embedder", lambda settings: None)
    with pytest.raises(Exception):
        index_documents(ctx, path=r"C:\Windows\System32")


def test_docs_profile_uses_only_known_tools():
    p = load_profile("docs")
    assert {"index_documents", "search_documents", "read_file"} <= set(p.tools)
    assert all(t in REGISTRY for t in p.tools)
    assert REGISTRY["search_documents"].permission == "read" and REGISTRY["index_documents"].permission == "read"


def test_llama_embedder_request_and_server_command(settings, tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [3.0, 4.0]} for i, _ in enumerate(body["input"])]})

    s = settings.model_copy(deep=True)
    s.embedding.url = "http://emb.test"
    emb = embedding.LlamaEmbedder(s, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert emb.embed_documents(["가", "나"]) == [[0.6, 0.8], [0.6, 0.8]]   # 길이 1 로 맞춘다 (코사인)
    q = emb.embed_query("노즐 교체 주기")
    assert q == [0.6, 0.8] and emb.dim == 2
    assert seen[0]["input"] == ["가", "나"]
    assert seen[1]["input"][0].startswith("Instruct: ") and seen[1]["input"][0].endswith("Query: 노즐 교체 주기")
    cmd = embedding.server_command(settings)
    assert "--embedding" in cmd and cmd[cmd.index("--pooling") + 1] == "last"
    assert cmd[cmd.index("-ngl") + 1] == "0"   # 기본은 CPU — 채팅 모델과 VRAM 을 다투지 않게
    assert cmd[cmd.index("-m") + 1].endswith(".gguf")


def test_get_embedder_is_none_without_model_file(settings):
    s = settings.model_copy(deep=True)
    s.embedding.model_path = str(Path(settings.workspace) / "없음.gguf")
    assert embedding.get_embedder(s) is None
    s.embedding.enabled = False
    assert embedding.get_embedder(s) is None
