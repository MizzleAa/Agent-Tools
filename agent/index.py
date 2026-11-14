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
VEC_WINDOW = 0.15   # 의미 검색: 가장 가까운 조각보다 이만큼 더 먼 것까지만 (진짜 Qwen3-Embedding 으로 잰 값 — 맞는 구역은 다음 것보다 보통 0.1 이상 가깝다)
_lock = threading.Lock()   # 수집(쓰기)과 검색(읽기)이 다른 스레드에서 동시에 일어날 수 있다


def chunk_text(text: str, size: int = CHUNK_CHARS, headings: bool = False) -> list[str]:
    """문단 경계를 지키며 size 자 안팎으로 자른다. headings 면 마크다운 제목(#)에서 새 조각을 시작한다 (문서 Q&A — 구역 단위로 찾게)."""
    chunks, buf = [], ""
    is_heading = re.compile(r"#{1,6}\s")
    for para in re.split(r"\n\s*\n|\n", text):
        para = para.strip()
        if not para:
            continue
        # 제목에서 끊되, 앞 조각이 제목뿐이면(# X100 바로 아래 ## 보증) 끊지 않고 이어 붙인다
        if headings and buf and is_heading.match(para) and not all(is_heading.match(x) for x in buf.split("\n")):
            chunks.append(buf)
            buf = ""
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


_JOSA = ("으로부터", "에서부터", "으로", "에서", "부터", "까지", "에게", "한테", "처럼", "이나", "이랑", "보다", "은", "는", "이", "가",
         "을", "를", "에", "의", "로", "와", "과", "도", "만", "요")
QUESTION_WORDS = {"언제", "어디", "누가", "누구", "무엇", "뭐", "뭔가", "무슨", "왜", "어떻게", "어떤", "얼마", "얼마나", "몇", "어느",
                  "하나요", "인가요", "있나요", "되나요", "했나요", "돼", "돼요", "해요", "해", "알려줘", "알려", "주세요", "줘", "좀"}


def ko_stem(term: str) -> str:
    """끝에 붙은 조사를 뗀다 ('노즐은' → '노즐', '언제부터' → '언제'). 두 글자 미만이 되면 그대로."""
    for j in _JOSA:
        if term.endswith(j) and len(term) - len(j) >= 2:
            return term[: -len(j)]
    return term


def _content_terms(query: str) -> list[str]:
    """의미 있는 낱말만 — 조사를 떼고 의문사 · 어미(언제 · 뭐 · 하나요 …)는 뺀다 (문서 Q&A 의 낱말 검색)."""
    out = [ko_stem(t) for t in _terms(query)]
    return list(dict.fromkeys(t for t in out if t and t not in QUESTION_WORDS))


def _terms(query: str) -> list[str]:
    terms = [t.strip("\"'.,?!()[]{}:;") for t in query.split()]
    return list(dict.fromkeys(t for t in terms if t))   # 순서 유지 + 중복 제거


def _load_vec(db: sqlite3.Connection) -> bool:
    """sqlite-vec 확장 (의미 검색). 없거나 못 불러오면 낱말 검색만 한다."""
    try:
        import sqlite_vec
        db.enable_load_extension(True)
        try:
            sqlite_vec.load(db)
        finally:
            db.enable_load_extension(False)
        return True
    except Exception:   # noqa: BLE001 - 확장을 못 쓰는 파이썬이면 낱말 검색만
        return False


def _blob(vec: list[float]) -> bytes:
    import sqlite_vec
    return sqlite_vec.serialize_float32(vec)


def _prefix_sql(prefix: str) -> tuple[str, list[Any]]:
    """source 가 prefix 로 시작하는 것 (LIKE 는 폴더 이름의 '_' · '%' 를 와일드카드로 보므로 쓰지 않는다)."""
    return ("substr(docs.source, 1, ?) = ?", [len(prefix), prefix]) if prefix else ("1", [])


class DocIndex:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.has_vec = _load_vec(self.db)
        with _lock:
            self.db.execute("pragma journal_mode=wal")
            self.db.executescript(SCHEMA)
            self.db.execute("create table if not exists meta (key text primary key, value text)")

    def close(self) -> None:
        self.db.close()

    # ---- 벡터 (chunk_vec: 조각 rowid → 임베딩, 코사인 거리) — 7단계 문서 Q&A
    def _vec_dim(self) -> int:
        row = self.db.execute("select value from meta where key='vec_dim'").fetchone()
        return int(row["value"]) if row else 0

    def _drop_vectors_of(self, doc_id: int) -> None:
        if self.has_vec and self._vec_dim():
            self.db.execute("delete from chunk_vec where rowid in (select rowid from chunks where doc_id=?)", (doc_id,))

    def add_vectors(self, items: list[tuple[int, list[float]]]) -> int:
        if not self.has_vec or not items:
            return 0
        dim = len(items[0][1])
        with _lock, self.db:
            if self._vec_dim() != dim:   # 처음이거나 임베딩 모델이 바뀌었다 → 새로 만든다 (다시 임베딩된다)
                self.db.execute("drop table if exists chunk_vec")
                self.db.execute(f"create virtual table chunk_vec using vec0(embedding float[{dim}] distance_metric=cosine)")
                self.db.execute("insert or replace into meta(key, value) values ('vec_dim', ?)", (str(dim),))
            for rid, vec in items:
                self.db.execute("delete from chunk_vec where rowid=?", (rid,))
                self.db.execute("insert into chunk_vec(rowid, embedding) values (?, ?)", (rid, _blob(vec)))
        return len(items)

    def pending_chunks(self, prefix: str = "", source: str | None = None, limit: int | None = None) -> list[tuple[int, str]]:
        """아직 임베딩이 없는 조각 [(rowid, 제목 + 본문)]."""
        if not self.has_vec:
            return []
        where, params = _prefix_sql(prefix)
        if source:
            where, params = "docs.source = ?", [source]
        missing = "and chunks.rowid not in (select rowid from chunk_vec)" if self._vec_dim() else ""
        sql = (f"select chunks.rowid rid, chunks.title, chunks.body from chunks join docs on docs.id = chunks.doc_id "
               f"where {where} {missing} order by chunks.rowid" + (f" limit {int(limit)}" if limit else ""))
        with _lock:
            return [(r["rid"], f"{r['title']}\n{r['body']}") for r in self.db.execute(sql, params)]

    def _vector_candidates(self, qvec: list[float], prefix: str, source: str | None, k: int,
                           max_distance: float | None = None) -> list[int]:
        """가까운 조각 rowid (가까운 순). max_distance: 코사인 거리가 이보다 먼 것은 뺀다 (관계없는 것까지 늘 나오지 않게)."""
        if not self.has_vec or not self._vec_dim() or len(qvec) != self._vec_dim():
            return []
        where, params = ("docs.source = ?", [source]) if source else _prefix_sql(prefix)
        with _lock:
            near = self.db.execute("select rowid, distance from chunk_vec where embedding match ? and k = ?",
                                   (_blob(qvec), max(k * 5, 50))).fetchall()
            if max_distance is not None and near:   # 아주 먼 것 · 가장 가까운 것보다 한참 먼 것은 뺀다
                best = near[0]["distance"]
                near = [r for r in near if r["distance"] <= min(max_distance, best + VEC_WINDOW)]
            if not near:
                return []
            ids = [r["rowid"] for r in near]
            ok = {r["rid"] for r in self.db.execute(
                f"select chunks.rowid rid from chunks join docs on docs.id = chunks.doc_id "
                f"where chunks.rowid in ({','.join('?' * len(ids))}) and {where}", ids + params)}
        return [i for i in ids if i in ok][:k]

    def upsert(self, source: str, url: str, title: str, text: str, chunk_chars: int = CHUNK_CHARS, headings: bool = False) -> bool:
        """페이지를 넣거나 바꾼다. 내용(과 자르는 방식)이 그대로면 시각만 갱신하고 False."""
        how = "" if (chunk_chars, headings) == (CHUNK_CHARS, False) else f"\x00{chunk_chars}:{headings}"   # 자르는 방식이 바뀌면 다시 자른다
        digest = hashlib.sha1((text + how).encode("utf-8")).hexdigest()
        now = datetime.now().isoformat(timespec="seconds")
        with _lock, self.db:
            row = self.db.execute("select id, hash from docs where url=?", (url,)).fetchone()
            if row and row["hash"] == digest:
                self.db.execute("update docs set fetched_at=?, source=? where id=?", (now, source, row["id"]))
                return False
            if row:
                doc_id = row["id"]
                self._drop_vectors_of(doc_id)
                self.db.execute("delete from chunks where doc_id=?", (doc_id,))
                self.db.execute("update docs set source=?, title=?, hash=?, chars=?, fetched_at=? where id=?",
                                (source, title, digest, len(text), now, doc_id))
            else:
                doc_id = self.db.execute(
                    "insert into docs(source, url, title, hash, chars, fetched_at) values (?,?,?,?,?,?)",
                    (source, url, title, digest, len(text), now)).lastrowid
            self.db.executemany("insert into chunks(title, body, doc_id) values (?,?,?)",
                                [(title, c, doc_id) for c in chunk_text(text, chunk_chars, headings)])
            return True

    def remove_missing(self, source: str, keep_urls: set[str]) -> int:
        """이번 수집에서 더는 보이지 않은 페이지(삭제된 페이지)를 색인에서 뺀다."""
        with _lock, self.db:
            gone = [r["id"] for r in self.db.execute("select id, url from docs where source=?", (source,))
                    if r["url"] not in keep_urls]
            for doc_id in gone:
                self._drop_vectors_of(doc_id)
                self.db.execute("delete from chunks where doc_id=?", (doc_id,))
                self.db.execute("delete from docs where id=?", (doc_id,))
            return len(gone)

    def has_url(self, url: str) -> bool:
        with _lock:
            return self.db.execute("select 1 from docs where url=?", (url,)).fetchone() is not None

    def count_docs(self, prefix: str = "", source: str | None = None) -> int:
        where, params = ("docs.source = ?", [source]) if source else _prefix_sql(prefix)
        with _lock:
            return self.db.execute(f"select count(*) from docs where {where}", params).fetchone()[0]

    def stats(self) -> dict[str, dict[str, Any]]:
        with _lock:
            rows = self.db.execute("select source, count(*) n, max(fetched_at) last from docs group by source")
            return {r["source"]: {"pages": r["n"], "last_fetched": r["last"]} for r in rows}

    def count(self) -> int:
        with _lock:
            return self.db.execute("select count(*) from docs").fetchone()[0]

    def search(self, query: str, source: str | None = None, limit: int = 8) -> list[dict[str, Any]]:
        best: dict[str, dict[str, Any]] = {}   # 문서(url)마다 가장 잘 맞는 조각 하나만
        for c in self._keyword_candidates(query, source=source):
            if c["url"] not in best or c["score"] > best[c["url"]]["score"]:
                best[c["url"]] = {k: c[k] for k in ("url", "title", "source", "score", "matched", "snippet")}
        ranked = sorted(best.values(), key=lambda x: x["score"], reverse=True)
        return ranked[:limit]

    def _keyword_candidates(self, query: str, source: str | None = None, prefix: str = "") -> list[dict[str, Any]]:
        """낱말이 하나라도 든 조각들 (점수 높은 순). source = 정확히 그 출처, prefix = 그 글자로 시작하는 출처들."""
        terms = _terms(query)
        if not terms:
            return []
        long_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]
        scope, scope_params = ("docs.source = ?", [source]) if source else _prefix_sql(prefix)
        select = ("select chunks.rowid rid, chunks.title, chunks.body, docs.url, docs.source, {score} score "
                  "from chunks join docs on docs.id = chunks.doc_id where {where} and " + scope + " limit 400")
        cands: dict[int, sqlite3.Row] = {}
        with _lock:
            if long_terms:
                fts = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
                sql = select.format(score="bm25(chunks)", where="chunks match ?")
                for r in self.db.execute(sql, [fts] + scope_params):
                    cands[r["rid"]] = r
            if short_terms:
                where = " or ".join(["chunks.body like ? or chunks.title like ?"] * len(short_terms))
                params = [p for t in short_terms for p in (f"%{t}%", f"%{t}%")]
                sql = select.format(score="0", where=f"({where})")
                for r in self.db.execute(sql, params + scope_params):
                    cands.setdefault(r["rid"], r)
        lowered = [t.lower() for t in terms]
        out = []
        for r in cands.values():
            body, title = r["body"].lower(), r["title"].lower()
            matched = sum(t in body or t in title for t in lowered)
            score = matched * 10 + sum(t in title for t in lowered) * 3 + min(-(r["score"] or 0), 5)
            out.append({"rid": r["rid"], "url": r["url"], "title": r["title"], "source": r["source"], "score": score,
                        "matched": matched, "body": r["body"], "snippet": _snippet(r["body"], lowered)})
        return sorted(out, key=lambda x: x["score"], reverse=True)

    def hybrid_search(self, query: str, qvec: list[float] | None, prefix: str = "", source: str | None = None,
                      limit: int = 6, per_doc: int = 2, max_distance: float | None = None,
                      snippet_chars: int = 360) -> list[dict[str, Any]]:
        """낱말(FTS5) + 의미(벡터) 를 RRF 로 합친 조각들. how = 낱말 · 의미 · 낱말+의미. qvec 이 없으면 낱말만.
        max_distance: 의미로만 찾은 조각의 코사인 거리 상한. snippet_chars: 조각에서 보여 줄 글자 (짧은 조각은 통째로)."""
        content = _content_terms(query)   # 질문 문장 그대로여도: 조사 떼고 의문사 빼고, 그 낱말의 절반 이상이 든 조각만
        need = max(1, -(-len(content) // 2))
        kw = [c for c in self._keyword_candidates(" ".join(content), source=source, prefix=prefix) if c["matched"] >= need] if content else []
        by_rid = {c["rid"]: c for c in kw}
        vec_ids = self._vector_candidates(qvec, prefix, source, k=max(limit * 3, 20), max_distance=max_distance) if qvec else []
        fused = rrf([[c["rid"] for c in kw], vec_ids])
        missing = [rid for rid in fused if rid not in by_rid]
        if missing:
            with _lock:
                for r in self.db.execute(
                        f"select chunks.rowid rid, chunks.title, chunks.body, docs.url, docs.source from chunks "
                        f"join docs on docs.id = chunks.doc_id where chunks.rowid in ({','.join('?' * len(missing))})", missing):
                    by_rid[r["rid"]] = {"rid": r["rid"], "url": r["url"], "title": r["title"], "source": r["source"],
                                        "body": r["body"], "snippet": _snippet(r["body"], [t.lower() for t in content])}
        kw_ids, vec_set = {c["rid"] for c in kw}, set(vec_ids)
        out: list[dict[str, Any]] = []
        per: dict[str, int] = {}
        for rank, rid in enumerate(fused):
            c = by_rid.get(rid)
            if c is None or per.get(c["url"], 0) >= per_doc:
                continue
            per[c["url"]] = per.get(c["url"], 0) + 1
            how = "낱말+의미" if rid in kw_ids and rid in vec_set else "낱말" if rid in kw_ids else "의미"
            snippet = c["snippet"] if snippet_chars <= 360 else _snippet(c["body"], [t.lower() for t in content], snippet_chars)
            out.append({"url": c["url"], "title": c["title"], "source": c["source"], "snippet": snippet,
                        "how": how, "rank": rank + 1})
            if len(out) >= limit:
                break
        return out


def rrf(rankings: list[list[int]], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion — 여러 순위 목록을 합친다 (둘 다에 높이 있으면 위로). 점수 단위가 달라도 된다."""
    score: dict[int, float] = {}
    for ranking in rankings:
        for i, item in enumerate(ranking):
            score[item] = score.get(item, 0.0) + 1.0 / (k + i + 1)
    return sorted(score, key=lambda x: score[x], reverse=True)


def _snippet(body: str, terms: list[str], width: int = 360) -> str:
    low = body.lower()
    hits = [i for i in (low.find(t) for t in terms) if i >= 0]
    start = max(0, min(hits) - 120) if hits else 0
    text = body[start:start + width].replace("\n", " ")
    return ("…" if start else "") + text + ("…" if start + width < len(body) else "")
