"""파일 도구: 목록 보기, 읽기, 찾기, 쓰기. 모든 경로는 Sandbox 를 거친다."""

from __future__ import annotations

import fnmatch
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field

from agent import documents, locations
from agent.tools.base import ToolContext, ToolError, decode_bytes, tool

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}
MAX_READ_BYTES = 2_000_000
MAX_SEARCH_FILE_BYTES = 1_000_000


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _is_link(path: str) -> bool:
    # os.walk 는 정션(junction)을 일반 폴더처럼 따라 들어간다. 정션이 허용 폴더 밖을 가리키면
    # 샌드박스를 우회하게 되므로 링크·정션은 건너뛴다.
    return os.path.islink(path) or os.path.isjunction(path)


LIST_SHOWN = 200          # 목록에 이름까지 보여 줄 항목 수 (넘으면 개수·크기 요약 + 찾기 안내 — 큰 공유 폴더에서 16k 를 넘지 않게)
LIST_SUMMARY = 30         # 이보다 많은 폴더만 머리줄에 개수·크기 합계·최근 수정 파일을 붙인다
SEARCH_SECONDS = 30       # 찾기 시간 한도 (수천 개 파일·네트워크 폴더에서 끝나게)
SEARCH_MAX_FILES = 50_000
CONTENT_MAX_FILES = 3_000
CONTENT_MAX_BYTES = 5 * 2**20


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _guard(path: str, fn):   # noqa: ANN001 — 네트워크 위치는 시간 제한 + 실패 갈래 (A6)
    try:
        return locations.run_with_timeout(fn) if locations.is_unc(str(path)) or locations._is_remote_drive(str(path)) else fn()
    except (OSError, TimeoutError) as e:
        raise ToolError(locations.explain(e, str(path)))


@tool("read")
def list_dir(
    ctx: ToolContext,
    path: Annotated[str, Field(description="폴더 경로. 작업 폴더 기준 상대 경로 가능 (공유 위치는 경로 그대로)")] = ".",
) -> str:
    """폴더 안의 파일과 하위 폴더 목록을 크기와 함께 보여준다."""
    p = ctx.sandbox.resolve(path)

    def scan() -> list[tuple[str, bool, int, float]]:
        if not p.is_dir():
            raise ToolError(f"폴더가 아닙니다: {path}" if p.exists() else locations.explain(FileNotFoundError(path), path))
        out = []
        with os.scandir(p) as it:
            for e in it:
                try:
                    st = e.stat()
                    out.append((e.name, e.is_dir(), 0 if e.is_dir() else st.st_size, st.st_mtime))
                except OSError:
                    out.append((e.name, e.is_dir(follow_symlinks=False), 0, 0.0))
        return out

    entries = sorted(_guard(path, scan), key=lambda e: (not e[1], e[0].lower()))
    files = [e for e in entries if not e[1]]
    dirs = len(entries) - len(files)
    if len(entries) > LIST_SUMMARY:   # 큰 폴더만 개수·합계·최근 수정을 미리 센다 (작은 폴더는 목록이 곧 답 — 머리줄이 길면 소형 모델이 더 돌아다닌다)
        lines = [f"{ctx.rel(p)}  ({len(entries)}개 항목: 폴더 {dirs}개 · 파일 {len(files)}개 · 파일 합계 {_human(sum(f[2] for f in files))})"]
        recent = sorted(files, key=lambda f: -f[3])[:5]
        if recent:
            lines.append("  최근 수정: " + ", ".join(f"{f[0]} ({_when(f[3])})" for f in recent))
    else:
        lines = [f"{ctx.rel(p)}  ({len(entries)}개 항목)"]
    for name, is_dir, size, mtime in entries[:LIST_SHOWN]:
        lines.append(f"  [D] {name}/" if is_dir else f"  [F] {name}  {_human(size)}  {_when(mtime) if mtime else ''}".rstrip())
    if len(entries) > LIST_SHOWN:
        lines.append(f"  ... {len(entries) - LIST_SHOWN}개 더 있음 (개수·합계는 위 요약이 전체 기준). "
                     "이름으로 찾으려면 search_files(path=…, name_pattern='*낱말*')")
    return "\n".join(lines)


@tool("read")
def read_file(
    ctx: ToolContext,
    path: Annotated[str, Field(description="파일 경로")],
    start_line: Annotated[int, Field(description="긴 파일을 이어 읽을 때만 쓴다. 처음 읽을 때는 지정하지 않는다", ge=1)] = 1,
    max_lines: Annotated[int, Field(description="최대 줄 수", ge=1, le=500)] = 200,
) -> str:
    """텍스트 파일을 줄 번호와 함께 처음부터 읽는다 (엑셀·PDF·워드는 글로 바꿔서). 200줄이 넘는 파일만 start_line 으로 이어 읽는다."""
    p = ctx.sandbox.resolve(path)

    def load() -> documents.Document:
        if not p.is_file():
            raise ToolError(f"파일이 아닙니다 (폴더면 list_dir): {path}" if p.is_dir() else locations.explain(FileNotFoundError(path), path))
        return documents.read(p)

    doc = _guard(path, load)
    if doc.kind == "image":
        return f"{ctx.rel(p)}: {doc.note} — {doc.fmt} {doc.width}×{doc.height}, {_human(p.stat().st_size)}"
    if doc.kind in ("binary", "too_big"):
        raise ToolError(f"{doc.note}: {path}")
    lines = doc.text.splitlines()
    end = min(len(lines), start_line - 1 + max_lines)
    body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start_line, end + 1))
    footer = f"\n({ctx.rel(p)}: 전체 {len(lines)}줄 중 {start_line}-{end}줄"
    footer += f", 다음은 start_line={end + 1})" if end < len(lines) else ")"
    if doc.note:
        footer += f"\n({doc.note})"
    read_heads = ctx.memo.setdefault("read_heads", set())
    if start_line == 1:
        read_heads.add(str(p))
    elif str(p) not in read_heads:
        # 소형 모델이 '헤더 제외' 같은 말에 2줄부터 읽고는 헤더를 지어내는 일이 있었다 (맨 앞을 아직 안 읽었을 때만 경고)
        footer += (f"\n⚠ 파일 맨 앞 1-{min(start_line - 1, len(lines))}줄(제목·헤더가 보통 여기 있음)은 아직 읽지 않았습니다. "
                   "그 내용이 필요하면 start_line 없이 다시 읽으세요.")
    return (body or "(빈 범위)") + footer


@tool("read")
def search_files(
    ctx: ToolContext,
    path: Annotated[str, Field(description="검색을 시작할 폴더")] = ".",
    name_pattern: Annotated[str, Field(description="파일 이름 패턴. 예: *.py, *report*")] = "*",
    text: Annotated[str, Field(description="파일 내용에서 찾을 문자열(대소문자 무시). 비우면 이름으로만 찾음")] = "",
) -> str:
    """하위 폴더까지 파일을 이름 패턴으로 찾고, text 를 주면 그 문자열이 들어있는 줄도 보여준다 (엑셀·PDF·워드 안도)."""
    root = ctx.sandbox.resolve(path)
    _guard(path, lambda: root.is_dir() or (_ for _ in ()).throw(
        ToolError(f"폴더가 아닙니다: {path}" if root.exists() else locations.explain(FileNotFoundError(path), path))))
    needle = text.lower()
    hits: list[str] = []
    named = scanned = found_files = walked = 0
    stopped = ""
    started = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not _is_link(os.path.join(dirpath, d))]
        for fname in filenames:
            walked += 1
            if walked > SEARCH_MAX_FILES or time.monotonic() - started > SEARCH_SECONDS:
                stopped = f"시간·개수 한도로 {walked - 1}개 파일까지만 봤습니다 — 범위(폴더·이름 패턴)를 좁혀 다시 찾으세요"
                break
            if not fnmatch.fnmatch(fname.lower(), name_pattern.lower()):
                continue
            fpath = Path(dirpath, fname)
            if _is_link(str(fpath)):
                continue
            named += 1
            try:
                size = fpath.stat().st_size
            except OSError:
                continue
            if not needle:
                if len(hits) < 100:
                    hits.append(f"{ctx.rel(fpath)}  {_human(size)}")
                continue
            ext = fpath.suffix.lower()
            readable = ext in documents.TEXT_EXT or ext in documents.OFFICE_EXT or not ext
            if not readable or size > (CONTENT_MAX_BYTES if ext in documents.OFFICE_EXT else MAX_SEARCH_FILE_BYTES) \
                    or scanned >= CONTENT_MAX_FILES:
                continue
            scanned += 1
            try:
                body = documents.read(fpath).text
            except OSError:
                continue
            got = False
            for no, line in enumerate(body.splitlines(), 1):
                if needle in line.lower():
                    got = True
                    if len(hits) < 100:
                        hits.append(f"{ctx.rel(fpath)}:{no}: {line.strip()[:200]}")
            found_files += got
        if stopped:
            break
    tail = f"\n⚠ {stopped}" if stopped else ""
    # 개수 요약은 결과가 다 보이지 않을 때만 (많거나·못 본 파일이 있거나·한도로 멈춤). 작은 결과는 목록이 곧 답이다
    if not (named > 100 or len(hits) >= 100 or stopped or (needle and scanned < named)):
        return ("\n".join(hits) if hits else "찾은 결과가 없습니다.") + tail
    if needle:
        head = f"(이름이 맞는 파일 {named}개 중 내용을 본 파일 {scanned}개, 찾는 글이 들어 있는 파일 {found_files}개)"
    else:
        head = f"(이름이 맞는 파일 {named}개)"
    if len(hits) >= 100:
        head += " — 보여 주는 줄은 앞 100개"
    return (head + "\n" + "\n".join(hits) if hits else f"찾은 결과가 없습니다. {head}") + tail


@tool("write")
def write_file(
    ctx: ToolContext,
    path: Annotated[str, Field(description="저장할 파일 경로")],
    content: Annotated[str, Field(description="파일에 쓸 전체 내용")],
    append: Annotated[bool, Field(description="true 면 기존 내용 뒤에 덧붙임")] = False,
) -> str:
    """텍스트 파일을 UTF-8 로 저장한다. 없는 폴더는 만든다."""
    p = ctx.sandbox.resolve(path, write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return f"저장 완료: {ctx.rel(p)} ({p.stat().st_size} bytes{', 덧붙임' if append else ''})"
