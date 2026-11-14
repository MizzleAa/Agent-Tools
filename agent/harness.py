"""하네스: 관리자가 마크다운으로 관리하는 공통 지침 · 역할 · 자료(사용설명서 · 에러 대처 같은 QA 자료).

    harness/
      AGENT.md                  공통 지침 — 모든 실행의 시스템 지침에 붙는다
      agents/<역할>.md           역할 (위쪽 --- yaml --- 에 name · description · tools … / 본문 = 역할 지침)
      <폴더>/…/*.md              자료. 에이전트는 목록만 보고 search_knowledge · read_knowledge 로 필요한 것만 꺼내 읽는다
      <폴더>/AGENT.md            (선택) 그 폴더의 지침 + 대상 회사 (companies: [회사 이름…]) — 비우면 모두. 아래 폴더도 따른다

모양은 Claude Code 의 마크다운 하네스와 같지만(--- yaml --- + 본문) 특정 공급사 이름을 쓰지 않는다 — 모델이 로컬이든
OpenAI · Claude · Gemini 든 같은 파일이 그대로 적용된다. 작업 폴더 안의 AGENT.md · AGENTS.md · CLAUDE.md · GEMINI.md 도
그 폴더에서 일할 때 지침으로 붙는다 (project_instructions).

대상 회사: 로그인 사용 모드의 일반 사용자는 자기 회사가 대상인 폴더(와 대상이 비어 있는 폴더)만 쓴다 (Settings.audience).

하네스 여러 개: 위 harness/ 가 '공통'이고, 회사·프로젝트 단위 하네스는 harnesses/<이름>/ 에 같은 모양으로 둔다.

    harnesses/<이름>/AGENT.md   머리말에 title · kind(company 회사 | project 프로젝트) · companies(대상 회사) + 본문 = 그 하네스 지침
    harnesses/<이름>/agents/    그 하네스의 용도(역할)
    harnesses/<이름>/<폴더>/    그 하네스의 자료

실행에는 공통 + 그 사람 회사가 대상인 회사 하네스 + 입력창·워크플로 노드에서 고른 하네스가 합쳐져 붙는다 (active).
같은 이름의 용도가 여러 하네스에 있으면 고른 하네스 → 회사 하네스 → 공통 순서로 찾는다. 자료 경로는 '@<이름>/폴더/파일.md'.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agent import config

HARNESS_DIR = config.ROOT / "harness"
AGENT_MD = "AGENT.md"
ROLES = "agents"
PROJECT_FILES = ("AGENT.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md")   # 작업 폴더 안의 지침 파일 (먼저 찾은 것부터)
MAX_FILE_BYTES = 2_000_000
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
# Claude Code 역할 파일의 도구 이름 → 이 에이전트의 도구
TOOL_ALIASES = {"read": "read_file", "write": "write_file", "edit": "write_file", "multiedit": "write_file",
                "grep": "search_files", "glob": "search_files", "ls": "list_dir", "bash": "run_command",
                "webfetch": "fetch_url", "websearch": "web_search"}
DEFAULT_ROLE_TOOLS = ["list_dir", "read_file", "search_files"]
SCOPES_DIR = "harnesses"
SCOPE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
KINDS = ("company", "project")


def scopes_dir() -> Path:
    """회사·프로젝트 하네스가 모인 폴더 (공통 harness/ 옆)."""
    return HARNESS_DIR.parent / SCOPES_DIR


def root(scope: str = "") -> Path:
    """'' = 공통 하네스, 그 밖에는 harnesses/<scope>."""
    if not scope:
        return HARNESS_DIR
    if not SCOPE_RE.match(scope):
        raise ValueError(f"하네스 이름이 올바르지 않습니다: {scope}")
    return scopes_dir() / scope


# ───────────────────────── 마크다운 + frontmatter

def split_front(text: str) -> tuple[dict[str, Any], str]:
    """'--- yaml ---' 머리말과 본문. 머리말이 없거나 깨졌으면 ({}, 전체) — 깨진 것은 check() 가 알려 준다."""
    m = _FRONT.match(text.lstrip("﻿"))
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    return (meta if isinstance(meta, dict) else {}), text.lstrip("﻿")[m.end():]


def join_front(meta: dict[str, Any], body: str) -> str:
    if not meta:
        return body
    head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{head}\n---\n\n{body.lstrip()}"


def check(text: str) -> str:
    """머리말 문법 오류 (없으면 빈 글자)."""
    m = _FRONT.match(text.lstrip("﻿"))
    if not m:
        return "" if not text.lstrip("﻿").startswith("---") else "머리말(---)이 닫히지 않았습니다"
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        return f"머리말 yaml 오류: {e}"
    return "" if meta is None or isinstance(meta, dict) else "머리말은 '이름: 값' 모양이어야 합니다"


# ───────────────────────── 경로

def safe(rel: str, *, file: bool = True, scope: str = "") -> Path:
    """하네스 안의 상대 경로 → 실제 경로. 밖으로 나가거나 이상한 이름이면 ValueError. 파일은 .md 만."""
    rel = (rel or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." or p.startswith(".") or re.search(r'[<>:"|?*\x00-\x1f]', p) for p in parts):
        raise ValueError(f"쓸 수 없는 경로입니다: {rel}")
    if file and (not parts or not parts[-1].lower().endswith(".md")):
        raise ValueError("하네스 파일은 .md 로 끝나야 합니다")
    top = root(scope)
    path = top.joinpath(*parts) if parts else top
    base = os.path.normcase(os.path.realpath(top))
    real = os.path.normcase(os.path.realpath(path))
    if real != base and not real.startswith(base + os.sep):
        raise ValueError("하네스 폴더 밖입니다")
    return path


def rel_of(path: Path, scope: str = "") -> str:
    return path.relative_to(root(scope)).as_posix()


def ref(scope: str, rel: str) -> str:
    """실행에서 쓰는 자료 경로: 공통은 그대로, 다른 하네스는 '@<이름>/경로'."""
    return f"@{scope}/{rel}" if scope else rel


def parse_ref(path: str) -> tuple[str, str]:
    path = (path or "").replace("\\", "/").strip()
    if path.startswith("@"):
        scope, _, rel = path[1:].partition("/")
        return scope, rel
    return "", path


# ───────────────────────── 목록 · 대상 회사

@dataclass
class Doc:
    path: str                      # 하네스 기준 상대 경로 (posix)
    folder: str                    # '' = 맨 위
    kind: str                      # instructions(AGENT.md) · role(agents/*.md) · doc(자료)
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    size: int = 0
    modified: str = ""
    error: str = ""
    scope: str = ""                # 어느 하네스 ('' = 공통)


@dataclass
class Folder:
    path: str
    companies: list[str] = field(default_factory=list)   # 이 폴더 AGENT.md 에 적은 대상 회사
    effective: list[str] = field(default_factory=list)   # 위 폴더에서 물려받은 것까지 (비면 모두)
    has_instructions: bool = False


def _title(meta: dict[str, Any], body: str, path: Path) -> str:
    if meta.get("title") or meta.get("name"):
        return str(meta.get("title") or meta.get("name"))
    m = _HEADING.search(body)
    return m.group(2).strip() if m else path.stem


def _companies(meta: dict[str, Any]) -> list[str]:
    raw = meta.get("companies") or meta.get("대상 회사") or []
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[,\n]", raw)]
    return [str(x).strip() for x in raw if str(x).strip()]


def scan(scope: str = "") -> tuple[list[Folder], list[Doc]]:
    base = root(scope)
    if not base.is_dir():
        return [Folder(path="")], []
    folders: dict[str, Folder] = {"": Folder(path="")}
    docs: list[Doc] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        here = Path(dirpath)
        rel = "" if here == base else rel_of(here, scope)
        folders.setdefault(rel, Folder(path=rel))
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            path = here / name
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = split_front(text)
            kind = "instructions" if name == AGENT_MD else "role" if rel == ROLES else "doc"
            st = path.stat()
            docs.append(Doc(path=rel_of(path, scope), folder=rel, kind=kind, title=_title(meta, body, path),
                            description=str(meta.get("description") or ""),
                            tags=[str(t) for t in (meta.get("tags") or [])] if isinstance(meta.get("tags"), list) else [],
                            size=st.st_size, modified=datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                            error=check(text), scope=scope))
            if kind == "instructions":
                folders[rel].has_instructions = True
                folders[rel].companies = _companies(meta)
    for rel, f in folders.items():   # 대상 회사는 가장 가까운 위 폴더의 설정을 따른다
        cur = rel
        while True:
            if folders.get(cur) and folders[cur].companies:
                f.effective = folders[cur].companies
                break
            if not cur:
                break
            cur = cur.rsplit("/", 1)[0] if "/" in cur else ""
    return sorted(folders.values(), key=lambda f: f.path), docs


def _visible(folder: Folder, audience: str | None) -> bool:
    """audience: None = 전부 (관리자 · 혼자 쓰기), 그 밖에는 회사 이름 ('' = 회사 없음 → 대상이 빈 폴더만)."""
    return audience is None or not folder.effective or audience in folder.effective


def visible_docs(audience: str | None, scopes: list[str] | None = None) -> list[Doc]:
    """scopes: 쓸 하네스들 (None = 공통만). 경로(path)는 각 하네스 안 상대 경로 — 실행에 보일 때는 ref()."""
    out: list[Doc] = []
    for sc in scopes or [""]:
        folders, docs = scan(sc)
        ok = {f.path for f in folders if _visible(f, audience)}
        out += [d for d in docs if d.folder in ok]
    return out


def has_docs(audience: str | None, scopes: list[str] | None = None) -> bool:
    return any(d.kind == "doc" for d in visible_docs(audience, scopes))


# ───────────────────────── 하네스 여러 개 (공통 + 회사 · 프로젝트)

@dataclass
class Scope:
    id: str
    title: str
    kind: str                                            # company · project
    companies: list[str] = field(default_factory=list)  # 대상 회사 (비면 모두). 회사 하네스는 그 회사 사람의 실행에 저절로 붙는다
    description: str = ""
    roles: list[str] = field(default_factory=list)      # 이 하네스의 용도(agents/*.md)


def _scope(sid: str) -> Scope:
    f = root(sid) / AGENT_MD
    meta = split_front(f.read_text(encoding="utf-8", errors="replace"))[0] if f.is_file() else {}
    kind = str(meta.get("kind") or "project")
    return Scope(id=sid, title=str(meta.get("title") or sid), kind=kind if kind in KINDS else "project",
                 companies=_companies(meta), description=str(meta.get("description") or ""), roles=role_names(sid))


def list_scopes() -> list[Scope]:
    d = scopes_dir()
    if not d.is_dir():
        return []
    return [_scope(p.name) for p in sorted(d.iterdir()) if p.is_dir() and SCOPE_RE.match(p.name)]


def get_scope(sid: str) -> Scope | None:
    return _scope(sid) if sid and SCOPE_RE.match(sid) and root(sid).is_dir() else None


def usable(sid: str, audience: str | None) -> bool:
    """그 사람이 고를 수 있는 하네스인지 (관리자 · 혼자 쓰기는 전부, 그 밖에는 대상이 비었거나 그 회사가 대상)."""
    if not sid:
        return True
    sc = get_scope(sid)
    return sc is not None and (audience is None or not sc.companies or audience in sc.companies)


def active(audience: str | None, chosen: str = "") -> list[str]:
    """이 실행에 붙는 하네스: 공통 + 그 사람 회사가 대상인 회사 하네스 + 고른 하네스 (없거나 못 쓰는 것은 뺀다)."""
    out = [""]
    if audience:
        out += [sc.id for sc in list_scopes() if sc.kind == "company" and audience in sc.companies]
    if chosen and chosen not in out and usable(chosen, audience):
        out.append(chosen)
    return out


def create_scope(sid: str, title: str, kind: str, companies: list[str] | None = None, description: str = "") -> Scope:
    if not SCOPE_RE.match(sid or ""):
        raise ValueError("하네스 이름은 영어 소문자·숫자·-·_ 로 32자 이하 (예: acme, project-x)")
    if kind not in KINDS:
        raise ValueError("종류는 company(회사) 또는 project(프로젝트)")
    if root(sid).exists():
        raise ValueError(f"이미 있는 하네스입니다: {sid}")
    meta: dict[str, Any] = {"title": title.strip() or sid, "kind": kind}
    if names := [c.strip() for c in companies or [] if c.strip()]:
        meta["companies"] = names
    if description.strip():
        meta["description"] = description.strip()
    (root(sid) / ROLES).mkdir(parents=True)
    body = "이 하네스의 지침 (이 하네스를 쓰는 모든 실행에 공통 지침 뒤에 붙는다).\n"
    (root(sid) / AGENT_MD).write_text(join_front(meta, body), encoding="utf-8")
    return _scope(sid)


def update_scope(sid: str, title: str | None = None, kind: str | None = None, companies: list[str] | None = None,
                 description: str | None = None) -> Scope:
    if get_scope(sid) is None:
        raise ValueError(f"없는 하네스입니다: {sid}")
    if kind is not None and kind not in KINDS:
        raise ValueError("종류는 company(회사) 또는 project(프로젝트)")
    f = root(sid) / AGENT_MD
    meta, body = split_front(f.read_text(encoding="utf-8")) if f.is_file() else ({}, "")
    for key, value in (("title", title), ("kind", kind), ("description", description)):
        if value is not None:
            meta[key] = value.strip() or (sid if key == "title" else "")
            if not meta[key]:
                meta.pop(key)
    if companies is not None:
        meta.pop("대상 회사", None)
        if names := [c.strip() for c in companies if c.strip()]:
            meta["companies"] = names
        else:
            meta.pop("companies", None)
    f.write_text(join_front(meta, body), encoding="utf-8")
    return _scope(sid)


def delete_scope(sid: str) -> None:
    if get_scope(sid) is None:
        raise ValueError(f"없는 하네스입니다: {sid}")
    shutil.rmtree(root(sid))


def _scope_titles(scopes: list[str]) -> dict[str, str]:
    return {sc: (s.title if (s := get_scope(sc)) else sc) for sc in scopes if sc}


# ───────────────────────── 실행에 쓰기

def instructions(audience: str | None, max_chars: int, scopes: list[str] | None = None) -> str:
    """공통 지침 + 그 사람이 쓰는 폴더의 AGENT.md (맨 위부터) + 붙는 하네스의 지침. 너무 길면 자르고 그렇다고 적는다."""
    parts = []
    titles = _scope_titles(scopes or [])
    for d in visible_docs(audience, scopes):
        if d.kind != "instructions":
            continue
        _, body = split_front(safe(d.path, scope=d.scope).read_text(encoding="utf-8", errors="replace"))
        if not body.strip():
            continue
        if not d.scope:
            parts.append(body.strip() if not d.folder else f"### {d.folder} 자료에 대한 지침\n{body.strip()}")
        else:
            head = f"하네스 '{titles.get(d.scope, d.scope)}'" + (f" › {d.folder} 자료에 대한" if d.folder else "")
            parts.append(f"### {head} 지침\n{body.strip()}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…(하네스 지침이 길어 뒷부분을 줄였습니다 — 관리자가 줄여야 합니다)"
    return text


def catalog(audience: str | None, max_items: int, scopes: list[str] | None = None) -> str:
    """시스템 지침에 붙일 자료 목록 (경로 — 제목: 설명). 본문은 넣지 않는다."""
    docs = [d for d in visible_docs(audience, scopes) if d.kind == "doc"]
    lines = [f"- {ref(d.scope, d.path)} — {d.title}" + (f": {d.description}" if d.description else "") for d in docs[:max_items]]
    if len(docs) > max_items:
        lines.append(f"- … 외 {len(docs) - max_items}개 (search_knowledge 로 찾는다)")
    return "\n".join(lines)


def _sections(body: str) -> list[tuple[str, str]]:
    """(제목, 내용) — 마크다운 제목(#) 단위로 나눈다."""
    out, title, start = [], "", 0
    for m in _HEADING.finditer(body):
        if m.start() > start:
            out.append((title, body[start:m.start()]))
        title, start = m.group(2).strip(), m.start()
    out.append((title, body[start:]))
    return [(t, s) for t, s in out if s.strip()]


def search(audience: str | None, query: str, folder: str = "", limit: int = 6,
           scopes: list[str] | None = None) -> list[dict[str, Any]]:
    """자료 검색: 낱말이 많이, 제목에 나올수록 위로. 에러 코드(E-104 같은)는 붙은 그대로도 찾는다."""
    q = query.strip().lower()
    terms = [t for t in re.split(r"[\s,]+", q) if len(t) >= 2] or ([q] if q else [])
    if not terms:
        return []
    fscope, folder = parse_ref(folder) if folder.strip().startswith("@") else (None, folder)
    folder = folder.replace("\\", "/").strip("/")
    hits = []
    for d in visible_docs(audience, scopes):
        if d.kind != "doc" or (fscope is not None and d.scope != fscope) \
                or (folder and not (d.folder == folder or d.folder.startswith(folder + "/"))):
            continue
        meta, body = split_front(safe(d.path, scope=d.scope).read_text(encoding="utf-8", errors="replace"))
        head = f"{d.title} {d.description} {' '.join(d.tags)}".lower()
        for title, text in _sections(body):
            low = text.lower()
            score = sum(low.count(t) for t in terms) + sum(3 for t in terms if t in title.lower() or t in head)
            if q in low:
                score += 5
            if score:
                lines = [ln.strip() for ln in text.splitlines() if any(t in ln.lower() for t in terms)]
                hits.append({"path": ref(d.scope, d.path), "title": d.title, "section": title, "score": score,
                             "snippet": " / ".join(lines)[:400]})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def read(audience: str | None, path: str, start: int = 0, max_chars: int = 6000,
         scopes: list[str] | None = None) -> tuple[str, int, int]:
    """(내용 조각, 다음 시작 위치(끝이면 -1), 전체 길이). 그 사람이 쓸 수 없는 폴더 · 이 실행에 붙지 않은 하네스면 ValueError."""
    scope, rel = parse_ref(path)
    if scope not in (scopes or [""]):
        raise ValueError(f"하네스 자료가 없습니다: {path}")
    p = safe(rel, scope=scope)
    folders, _ = scan(scope)
    folder = rel_of(p.parent, scope) if p.parent != root(scope) else ""
    f = next((x for x in folders if x.path == folder), None)
    if not p.is_file() or f is None or not _visible(f, audience):
        raise ValueError(f"하네스 자료가 없습니다: {path}")
    _, body = split_front(p.read_text(encoding="utf-8", errors="replace"))
    chunk = body[start:start + max_chars]
    nxt = start + max_chars if start + max_chars < len(body) else -1
    return chunk, nxt, len(body)


def project_instructions(workdir: Path | None, stop_at: Path | None, max_chars: int) -> str:
    """작업 폴더(와 그 위 — stop_at 까지)에 둔 AGENT.md · AGENTS.md · CLAUDE.md · GEMINI.md. 가까운 폴더 것이 아래(나중)에 온다."""
    if workdir is None:
        return ""
    found: list[tuple[Path, str]] = []
    cur, stop = Path(workdir), Path(stop_at) if stop_at else None
    for _ in range(8):
        for name in PROJECT_FILES:
            f = cur / name
            if f.is_file() and f.stat().st_size <= MAX_FILE_BYTES:
                found.append((f, f.read_text(encoding="utf-8", errors="replace")))
                break   # 한 폴더에서는 하나만 (AGENT.md 가 먼저)
        if stop is None or os.path.normcase(str(cur)) == os.path.normcase(str(stop)) or cur.parent == cur:
            break
        cur = cur.parent
    parts = [f"### {f.parent.name or f.parent}/{f.name}\n{split_front(t)[1].strip()}" for f, t in reversed(found) if t.strip()]
    text = "\n\n".join(parts)
    return text[:max_chars] + ("\n…(지침 파일이 길어 뒷부분을 줄였습니다)" if len(text) > max_chars else "")


# ───────────────────────── 역할 (agents/*.md)

def role_names(scope: str = "") -> list[str]:
    d = root(scope) / ROLES
    return sorted(p.stem for p in d.glob("*.md")) if d.is_dir() else []


def role_text(name: str, scope: str = "") -> str | None:
    p = root(scope) / ROLES / f"{name}.md"
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None


def find_role(name: str, scopes: list[str] | None = None) -> tuple[str, str] | None:
    """(어느 하네스, 역할 파일 글). 뒤(고른 하네스)부터 찾는다 — 같은 이름이면 고른 하네스 → 회사 하네스 → 공통."""
    if not name or name.startswith(".") or re.search(r'[\\/<>:"|?*\x00-\x1f]', name):
        return None
    for sc in reversed(scopes or [""]):
        if (text := role_text(name, sc)) is not None:
            return sc, text
    return None


def all_role_names(scopes: list[str] | None = None) -> list[str]:
    return sorted({n for sc in scopes or [""] for n in role_names(sc)})


def tools_from(meta_tools: Any) -> tuple[list[str], list[str]]:
    """머리말의 tools (목록 또는 'Read, Grep' 같은 쉼표 글) → (이 에이전트 도구, 모르는 이름)."""
    from agent.tools import REGISTRY
    items = meta_tools if isinstance(meta_tools, list) else re.split(r"[,\s]+", str(meta_tools or ""))
    out, unknown = [], []
    for raw in (str(x).strip() for x in items):
        if not raw:
            continue
        name = raw if raw in REGISTRY else TOOL_ALIASES.get(raw.lower(), raw)
        if name in REGISTRY:
            if name not in out:
                out.append(name)
        else:
            unknown.append(raw)
    return out, unknown


def role_profile(name: str, text: str) -> dict[str, Any]:
    """역할 파일 → Profile 에 넣을 값. 머리말에 없는 도구 이름은 빼고, 도구가 비면 읽기 도구만."""
    meta, body = split_front(text)
    tools, _ = tools_from(meta.get("tools"))
    data: dict[str, Any] = {"name": name, "description": str(meta.get("description") or ""),
                            "system_prompt": body.strip() + "\n", "tools": tools or list(DEFAULT_ROLE_TOOLS)}
    for key in ("blocks", "max_steps", "think", "temperature", "top_p", "auto_approve_writes", "standards", "variables"):
        if key in meta and meta[key] is not None:
            data[key] = meta[key]
    return data


def role_markdown(profile: Any) -> str:
    """Profile → 역할 마크다운 (기존 용도를 하네스로 옮길 때)."""
    data = profile.model_dump()
    meta = {"name": data["name"], "description": data.get("description") or "", "tools": data["tools"]}
    for key in ("blocks", "max_steps", "think", "temperature", "top_p", "auto_approve_writes", "standards", "variables"):
        if data.get(key) not in (None, {}, False) or (key == "think" and data.get(key) is False):
            meta[key] = data[key]
    return join_front(meta, data.get("system_prompt") or "")


# ───────────────────────── 고치기 (관리 화면)

def _check_scope(scope: str) -> None:
    if scope and get_scope(scope) is None:
        raise ValueError(f"없는 하네스입니다: {scope}")


def write(rel: str, text: str, scope: str = "") -> Doc:
    _check_scope(scope)
    path = safe(rel, scope=scope)
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"파일이 너무 큽니다 ({MAX_FILE_BYTES // 1_000_000}MB 까지). 여러 파일로 나누세요")
    if err := check(text):
        raise ValueError(err)
    if path.parent.name == ROLES and path.parent.parent == root(scope):
        _check_role(path.stem, text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
    return next(d for d in scan(scope)[1] if d.path == rel_of(path, scope))


def _check_role(name: str, text: str) -> None:
    from agent.profiles import NAME_RE, Profile
    if not NAME_RE.match(name):
        raise ValueError("역할 파일 이름은 영어 소문자·숫자·-·_ 로 (예: agents/support.md)")
    meta, body = split_front(text)
    if not body.strip():
        raise ValueError("역할 지침(본문)이 비어 있습니다")
    _, unknown = tools_from(meta.get("tools"))
    if unknown:
        raise ValueError(f"모르는 도구가 있습니다: {', '.join(unknown)}")
    Profile.model_validate(role_profile(name, text))


def delete(rel: str, scope: str = "") -> None:
    _check_scope(scope)
    path = safe(rel, file=False, scope=scope)
    if path == root(scope):
        raise ValueError("하네스 폴더 자체는 지울 수 없습니다 (회사·프로젝트 하네스는 '하네스 지우기'로)")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.is_file() and path.suffix.lower() == ".md":
        path.unlink()
    else:
        raise ValueError(f"없는 파일·폴더입니다: {rel}")


def mkdir(rel: str, scope: str = "") -> Folder:
    _check_scope(scope)
    path = safe(rel, file=False, scope=scope)
    path.mkdir(parents=True, exist_ok=True)
    return next(f for f in scan(scope)[0] if f.path == ("" if path == root(scope) else rel_of(path, scope)))


def rename(src: str, dst: str, scope: str = "") -> None:
    _check_scope(scope)
    a, b = safe(src, file=False, scope=scope), safe(dst, file=False, scope=scope)
    if not a.exists():
        raise ValueError(f"없는 파일·폴더입니다: {src}")
    if a.is_file() and b.suffix.lower() != ".md":
        raise ValueError("하네스 파일은 .md 로 끝나야 합니다")
    if b.exists():
        raise ValueError(f"이미 있습니다: {dst}")
    b.parent.mkdir(parents=True, exist_ok=True)
    a.rename(b)


def set_companies(folder: str, companies: list[str], scope: str = "") -> Folder:
    """폴더 AGENT.md 의 대상 회사를 바꾼다 (없으면 만든다). 빈 목록 = 모두."""
    _check_scope(scope)
    path = safe(f"{folder}/{AGENT_MD}" if folder.strip("/") else AGENT_MD, scope=scope)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    meta, body = split_front(text)
    names = [c.strip() for c in companies if c.strip()]
    if names:
        meta["companies"] = names
    else:
        meta.pop("companies", None)
        meta.pop("대상 회사", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(join_front(meta, body) if meta else body, encoding="utf-8")
    return next(f for f in scan(scope)[0] if f.path == folder.strip("/"))


def import_file(folder: str, name: str, data: bytes, scope: str = "") -> Doc:
    """올린 파일을 자료 마크다운으로 (md·txt 는 그대로, PDF·워드·엑셀·CSV 는 글로 바꿔서)."""
    from agent import documents
    stem, suffix = os.path.splitext(os.path.basename(name.replace("\\", "/")))
    suffix = suffix.lower()
    if suffix in (".md", ".markdown", ".txt"):
        text = documents.decode(data)
    else:
        doc = documents.from_bytes(data, suffix)
        if doc.kind not in ("text", "table", "document") or not doc.text.strip():
            raise ValueError(f"'{name}' 에서 글을 꺼내지 못했습니다 ({doc.note or doc.kind})")
        text = join_front({"title": stem, "source": name}, f"# {stem}\n\n{doc.text}")
    safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(". ") or "자료"
    return write(f"{folder.strip('/')}/{safe_stem}.md" if folder.strip("/") else f"{safe_stem}.md", text, scope)


def asdicts(items: list[Any]) -> list[dict[str, Any]]:
    return [asdict(x) for x in items]
