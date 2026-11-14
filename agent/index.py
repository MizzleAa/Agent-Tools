"""사내 문서 색인 (data/index.db). 수집한 페이지를 조각(chunk)으로 나눠 SQLite FTS5 에 넣고 검색한다.

한국어 검색 요령
- FTS5 trigram 토크나이저는 3글자 이상이면 단어 중간도 찾는다 ("생산설비" 안의 "산설비").
- 그런데 '설비', '로트' 같은 2글자 단어는 trigram 으로 못 찾으므로 LIKE 로 따로 찾는다.
- 두 결과를 합친 뒤 "검색어 몇 개가 들어 있나"로 다시 점수를 매긴다. 수만 조각 정도는 이걸로 충분하다.
(나중에 임베딩 기반 의미 검색을 같은 DB 에 더할 수 있다 — 7단계)
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
create table if not exists docs (
    id integer primary key,
    source text not null,
    url text not null unique,
    title text not null default '',
    hash text not null,
    chars integer not null default 0,
    fetched_at text not null
);
create virtual table if not exists chunks using fts5(title, body, doc_id UNINDEXED, tokenize='trigram');
"""
CHUNK_CHARS = 1200
_lock = threading.Lock()   # 수집(쓰기)과 검색(읽기)이 다른 스레드에서 동시에 일어날 수 있다


def chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """문단 경계를 지키며 size 자 안팎으로 자른다."""
    chunks, buf = [], ""
    for para in re.split(r"\n\s*\n|\n", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:            # 아주 긴 문단은 강제로 자른다
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:size])
            para = para[size:]
        if len(buf) + len(para) + 1 > size and buf:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def _terms(query: str) -> list[str]:
    terms = [t.strip("\"'.,?!()[]{}:;") for t in query.split()]
    return list(dict.fromkeys(t for t in terms if t))   # 순서 유지 + 중복 제거


class DocIndex:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with _lock:
            self.db.execute("pragma journal_mode=wal")
            self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def upsert(self, source: str, url: str, title: str, text: str) -> bool:
        """페이지를 넣거나 바꾼다. 내용이 그대로면 시각만 갱신하고 False."""
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        now = datetime.now().isoformat(timespec="seconds")
        with _lock, self.db:
            row = self.db.execute("select id, hash from docs where url=?", (url,)).fetchone()
            if row and row["hash"] == digest:
                self.db.execute("update docs set fetched_at=?, source=? where id=?", (now, source, row["id"]))
                return False
            if row:
                doc_id = row["id"]
                self.db.execute("delete from chunks where doc_id=?", (doc_id,))
                self.db.execute("update docs set source=?, title=?, hash=?, chars=?, fetched_at=? where id=?",
                                (source, title, digest, len(text), now, doc_id))
            else:
                doc_id = self.db.execute(
                    "insert into docs(source, url, title, hash, chars, fetched_at) values (?,?,?,?,?,?)",
                    (source, url, title, digest, len(text), now)).lastrowid
            self.db.executemany("insert into chunks(title, body, doc_id) values (?,?,?)",
                                [(title, c, doc_id) for c in chunk_text(text)])
            return True

    def remove_missing(self, source: str, keep_urls: set[str]) -> int:
        """이번 수집에서 더는 보이지 않은 페이지(삭제된 페이지)를 색인에서 뺀다."""
        with _lock, self.db:
            gone = [r["id"] for r in self.db.execute("select id, url from docs where source=?", (source,))
                    if r["url"] not in keep_urls]
            for doc_id in gone:
                self.db.execute("delete from chunks where doc_id=?", (doc_id,))
                self.db.execute("delete from docs where id=?", (doc_id,))
            return len(gone)

    def stats(self) -> dict[str, dict[str, Any]]:
        with _lock:
            rows = self.db.execute("select source, count(*) n, max(fetched_at) last from docs group by source")
            return {r["source"]: {"pages": r["n"], "last_fetched": r["last"]} for r in rows}

    def count(self) -> int:
        with _lock:
            return self.db.execute("select count(*) from docs").fetchone()[0]

    def search(self, query: str, source: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        terms = _terms(query)
        if not terms:
            return []
        long_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]
        select = ("select chunks.rowid rid, chunks.title, chunks.body, docs.url, docs.source, {score} score "
                  "from chunks join docs on docs.id = chunks.doc_id where {where}"
                  + (" and docs.source = ?" if source else "") + " limit 400")
        cands: dict[int, sqlite3.Row] = {}
        with _lock:
            if long_terms:
                fts = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
                sql = select.format(score="bm25(chunks)", where="chunks match ?")
                for r in self.db.execute(sql, [fts] + ([source] if source else [])):
                    cands[r["rid"]] = r
            if short_terms:
                where = " or ".join(["chunks.body like ? or chunks.title like ?"] * len(short_terms))
                params = [p for t in short_terms for p in (f"%{t}%", f"%{t}%")]
                sql = select.format(score="0", where=f"({where})")
                for r in self.db.execute(sql, params + ([source] if source else [])):
                    cands.setdefault(r["rid"], r)

        best: dict[str, dict[str, Any]] = {}   # 문서(url)마다 가장 잘 맞는 조각 하나만
        lowered = [t.lower() for t in terms]
        for r in cands.values():
            body, title = r["body"].lower(), r["title"].lower()
            matched = sum(t in body or t in title for t in lowered)
            score = matched * 10 + sum(t in title for t in lowered) * 3 + min(-(r["score"] or 0), 5)
            if r["url"] not in best or score > best[r["url"]]["score"]:
                best[r["url"]] = {"url": r["url"], "title": r["title"], "source": r["source"], "score": score,
                                  "matched": matched, "snippet": _snippet(r["body"], lowered)}
        ranked = sorted(best.values(), key=lambda x: x["score"], reverse=True)
        return ranked[:limit]


def _snippet(body: str, terms: list[str], width: int = 360) -> str:
    low = body.lower()
    hits = [i for i in (low.find(t) for t in terms) if i >= 0]
    start = max(0, min(hits) - 120) if hits else 0
    text = body[start:start + width].replace("\n", " ")
    return ("…" if start else "") + text + ("…" if start + width < len(body) else "")
