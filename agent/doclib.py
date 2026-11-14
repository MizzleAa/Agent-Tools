"""7단계 문서 Q&A — 폴더의 문서를 색인(data/index.db)에 넣는다. 사내 사이트 색인(agent/crawler.py)과 같은 DB, 출처만 'docs:<폴더>'.

- 문서는 agent/documents.py 로 글로 바꾼다 (PDF · 워드 · 엑셀 · CSV · MD · TXT …). 바뀐 파일만 다시 읽고, 지운 파일은 뺀다.
- 조각마다 임베딩(agent/embedding.py)을 붙여 의미 검색을 한다. 임베딩을 못 쓰면 낱말 검색(FTS5)만.
- 검색은 DocIndex.hybrid_search (낱말 + 의미를 RRF 로).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent import documents
from agent.embedding import Embedder, EmbeddingError
from agent.index import DocIndex, rrf  # noqa: F401  (rrf: 테스트 · 다른 모듈이 doclib 에서 쓴다)

SOURCE_PREFIX = "docs:"
SUPPORTED = {".md", ".markdown", ".txt", ".text", ".rst", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml", ".html", ".htm",
             ".pdf", ".docx", ".xlsx"}
MAX_FILES = 3000
EMBED_BATCH = 64
CHUNK_CHARS = 600   # 사내 페이지(1200)보다 작게 + 마크다운 제목에서 끊는다 — 질문에 맞는 구역이 통째로 찾히게
SNIPPET_CHARS = 700


def source_for(folder: Path) -> str:
    return SOURCE_PREFIX + os.path.normcase(os.path.realpath(folder))


def _files(folder: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".venv"))
        out += [Path(dirpath) / f for f in sorted(filenames) if Path(f).suffix.lower() in SUPPORTED and not f.startswith("~$")]
    return out


def embed_pending(index: DocIndex, embedder: Embedder, prefix: str = SOURCE_PREFIX, source: str | None = None) -> int:
    """아직 임베딩이 없는 조각을 임베딩한다. 몇 조각 했는지."""
    done = 0
    while True:
        batch = index.pending_chunks(prefix, source=source, limit=EMBED_BATCH)
        if not batch:
            return done
        vecs = embedder.embed_documents([text for _, text in batch])
        done += index.add_vectors([(rid, v) for (rid, _), v in zip(batch, vecs)])


def index_folder(index: DocIndex, folder: Path, embedder: Embedder | None = None, max_files: int = MAX_FILES) -> dict[str, Any]:
    src = source_for(folder)
    files = _files(folder)
    r: dict[str, Any] = {"folder": str(folder), "source": src, "files": min(len(files), max_files), "truncated": len(files) > max_files,
                         "added": 0, "updated": 0, "unchanged": 0, "removed": 0, "failed": [], "embedded": 0,
                         "vector": False, "vector_error": ""}
    files = files[:max_files]
    for p in files:
        try:
            doc = documents.read(p)
        except OSError as e:
            r["failed"].append(f"{p.name}: {e}")
            continue
        if not doc.text.strip():
            r["failed"].append(f"{p.name}: {doc.note or '글자가 없습니다 (스캔한 PDF 등)'}")
            continue
        existed = index.has_url(str(p))
        if index.upsert(src, str(p), p.name, doc.text, chunk_chars=CHUNK_CHARS, headings=True):
            r["updated" if existed else "added"] += 1
        else:
            r["unchanged"] += 1
    r["removed"] = index.remove_missing(src, {str(p) for p in files})   # 이번에 못 읽은 파일은 남겨 둔다 (지운 것만 뺀다)
    if embedder is not None and index.has_vec:
        try:
            r["embedded"] = embed_pending(index, embedder, source=src)
            r["vector"] = True
        except EmbeddingError as e:
            r["vector_error"] = str(e)
    elif embedder is not None:
        r["vector_error"] = "sqlite-vec 확장을 불러오지 못했습니다"
    return r
