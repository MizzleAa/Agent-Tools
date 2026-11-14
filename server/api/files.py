"""작업 폴더 보기 (읽기 전용): 대화마다 생긴 작업 폴더 목록 · 폴더 안 파일 트리 · 파일 내용.

에이전트가 만든 코드를 화면에서 바로 본다 ('작업 폴더' 화면, 대화의 '코드 보기').
workspace 바로 아래 폴더만 열 수 있고, 경로는 실제 위치(realpath)로 확인해 그 폴더 밖은 읽지 않는다.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field

from agent.session import LOG_NAME
from server import deps
from server.api.common import ERRORS, ApiModel

router = APIRouter(tags=["files"], responses=ERRORS)

HEAVY = {".venv", "venv", "env", "node_modules", "__pycache__", ".git", ".pytest_cache", ".mypy_cache", "bin", "obj",
         ".idea", ".vs"}
MAX_ENTRIES = 3000
MAX_TEXT = 512 * 1024
LANG = {".py": "python", ".pyw": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript", ".cs": "csharp", ".csproj": "xml",
        ".sln": "text", ".json": "json", ".md": "markdown", ".html": "html", ".css": "css", ".xml": "xml",
        ".xaml": "xml", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ps1": "powershell", ".txt": "text",
        ".java": "java", ".go": "go", ".rs": "rust", ".sql": "sql", ".sh": "shell", ".bat": "shell", ".ini": "ini"}


class FolderInfo(ApiModel):
    name: str
    title: str = Field(description="작업기록.md 의 제목 (없으면 폴더 이름)")
    modified: str
    has_log: bool


class FileEntry(ApiModel):
    path: str = Field(description="폴더 기준 상대 경로 (/ 로 구분)")
    dir: bool
    size: int = 0
    modified: str = ""
    skipped: bool = Field(default=False, description="가상환경·패키지 폴더처럼 너무 커서 안을 펼치지 않은 폴더")


class FolderTree(ApiModel):
    name: str
    path: str
    entries: list[FileEntry]
    truncated: bool = Field(description=f"파일이 너무 많아 {MAX_ENTRIES}개까지만 보여 줌")


class FileContent(ApiModel):
    path: str
    size: int
    language: str
    binary: bool
    truncated: bool
    text: str | None = None


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _workspace() -> Path:
    """보는 사람의 작업 폴더 뿌리 — 관리자(·혼자 쓰기)는 workspace 전체, 로그인한 일반 사용자는 workspace/users/<아이디>."""
    return Path(os.path.realpath(deps.view_settings(deps.user()).workspace_path))


def _folder(name: str) -> Path:
    root = _workspace()
    if not name or re.search(r"[\\/]|^\.\.?$", name):
        raise HTTPException(404, "작업 폴더가 없습니다")
    folder = Path(os.path.realpath(root / name))
    if folder.parent != root or not folder.is_dir():
        raise HTTPException(404, "작업 폴더가 없습니다")
    return folder


def _inside(folder: Path, rel: str) -> Path:
    target = Path(os.path.realpath(folder / rel))
    if not target.is_relative_to(folder):
        raise HTTPException(404, "폴더 밖은 볼 수 없습니다")
    return target


def _title(folder: Path) -> str:
    log = folder / LOG_NAME
    if log.is_file():
        try:
            with log.open(encoding="utf-8", errors="replace") as f:
                first = f.readline().strip()
            if first.startswith("#"):
                return first.lstrip("# ").strip() or folder.name
        except OSError:
            pass
    return folder.name


@router.get("/workspace/folders", response_model=list[FolderInfo], summary="작업 폴더 목록 (최근 것부터)")
def folders() -> list[FolderInfo]:
    root = _workspace()
    if not root.is_dir():
        return []
    out = []
    for p in root.iterdir():
        if p.is_dir() and not p.name.startswith("."):
            out.append(FolderInfo(name=p.name, title=_title(p), modified=_when(p.stat().st_mtime), has_log=(p / LOG_NAME).is_file()))
    return sorted(out, key=lambda f: (f.has_log, f.name), reverse=True)


@router.get("/workspace/folders/{name}/tree", response_model=FolderTree, summary="폴더 안 파일 트리")
def tree(name: str) -> FolderTree:
    folder = _folder(name)
    entries: list[FileEntry] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        dirnames.sort(key=str.lower)
        keep = []
        for d in dirnames:
            p = here / d
            rel = p.relative_to(folder).as_posix()
            heavy = d in HEAVY or p.is_symlink() or not Path(os.path.realpath(p)).is_relative_to(folder)
            entries.append(FileEntry(path=rel, dir=True, skipped=heavy))
            if not heavy:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames, key=str.lower):
            p = here / f
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append(FileEntry(path=p.relative_to(folder).as_posix(), dir=False, size=st.st_size, modified=_when(st.st_mtime)))
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
    return FolderTree(name=folder.name, path=str(folder), entries=entries[:MAX_ENTRIES], truncated=truncated)


@router.get("/workspace/folders/{name}/file", response_model=FileContent, summary="파일 내용 (글자 파일만, 512KB 까지)")
def file(name: str, path: str = Query(..., description="폴더 기준 상대 경로")) -> FileContent:
    folder = _folder(name)
    target = _inside(folder, path)
    if not target.is_file():
        raise HTTPException(404, "파일이 없습니다")
    size = target.stat().st_size
    rel = target.relative_to(folder).as_posix()
    language = LANG.get(target.suffix.lower(), "text")
    with target.open("rb") as f:
        data = f.read(MAX_TEXT + 1)
    truncated = len(data) > MAX_TEXT
    data = data[:MAX_TEXT]
    if b"\x00" in data[:8192]:
        return FileContent(path=rel, size=size, language=language, binary=True, truncated=False)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp949")   # 한국어 Windows 기본 인코딩으로 저장된 파일
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
    return FileContent(path=rel, size=size, language=language, binary=False, truncated=truncated, text=text)
