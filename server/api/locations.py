"""파일 탐색기 (기능 '공유 경로·파일 탐색' B1~B7): 위치 목록 · 폴더 목록(나눠 받기·정렬·찾기) · 파일 미리보기 · 공유 위치 관리.

- 위치 = 작업 폴더(workspace) + 사용자가 허용한 공유 위치(config/shares.yaml, 읽기 전용). 이 밖은 화면으로도 열 수 없다 (B7).
- 모든 경로는 채팅 도구와 같은 샌드박스(agent/safety.py)를 거친다 — 허용 목록 밖·시스템 폴더·에이전트 설정·비밀 저장소·모델은 거절.
- 네트워크 위치는 시간 제한 안에서 읽고, 실패는 갈래(경로 없음·권한 없음·연결 안 됨·별도 계정 필요)로 알린다 (A6).
- 공유 계정은 넣기만 한다 — 응답·주소·기록에 돌려주지 않는다 (결정 ④).
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import Field

from agent import documents, imaging, locations
from agent.safety import Sandbox, SandboxError
from server import deps
from server.api.common import ERRORS, ApiModel, Ok

router = APIRouter(tags=["locations"], responses=ERRORS)

PAGE_MAX = 500                 # 한 번에 보내는 목록 항목 수 (큰 폴더는 나눠 받는다 — B5)
TEXT_LIMIT = 512 * 1024        # 미리보기 글자 수
TABLE_ROWS = 500               # 표 미리보기 행 수
RAW_MAX = 60 * 2**20           # 이미지 미리보기 최대 크기
VIEW_SIDE = 1600
LANG = {".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript", ".cs": "csharp",
        ".json": "json", ".md": "markdown", ".html": "html", ".css": "css", ".xml": "xml", ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml", ".ps1": "powershell", ".sql": "sql", ".sh": "shell", ".bat": "shell", ".ini": "ini", ".java": "java",
        ".go": "go", ".rs": "rust", ".c": "c", ".h": "c", ".cpp": "cpp"}


class LocationInfo(ApiModel):
    id: str
    kind: Literal["workspace", "share"]
    label: str
    path: str = Field(description="에이전트가 도는 이 PC 기준 경로")
    added: str = ""
    has_account: bool = Field(default=False, description="별도 계정이 저장돼 있는지 (값은 돌려주지 않는다)")
    writable: bool = False


class ShareAdd(ApiModel):
    path: str = Field(description="\\\\서버\\공유\\폴더 · Z:\\폴더 · file:// 링크")


class ShareAccount(ApiModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class DirEntry(ApiModel):
    name: str
    dir: bool
    size: int = 0
    modified: str = ""
    type: str = Field(default="", description="확장자 (폴더면 빈 글자)")


class DirListing(ApiModel):
    location: str
    path: str = Field(description="위치 기준 상대 경로 (/ 로 구분, 맨 위는 빈 글자)")
    abs_path: str = Field(description="이 PC 기준 전체 경로 (경로 복사용)")
    entries: list[DirEntry]
    total: int = Field(description="찾기 조건에 맞는 전체 항목 수")
    folders: int
    files: int
    offset: int
    limit: int


class FilePreview(ApiModel):
    location: str
    path: str
    abs_path: str
    name: str
    size: int
    modified: str
    kind: Literal["text", "table", "document", "image", "binary", "too_big"]
    language: str = "text"
    text: str | None = None
    rows: list[list[str]] = Field(default_factory=list)
    sheets: list[str] = Field(default_factory=list)
    note: str = ""
    width: int = 0
    height: int = 0


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _sandbox() -> Sandbox:
    return Sandbox.from_settings(deps.view_settings(deps.user()))


def _roots() -> list[LocationInfo]:
    s = deps.view_settings(deps.user())
    out = [LocationInfo(id="workspace", kind="workspace", label="작업 폴더", path=str(s.workspace_path), writable=True)]
    for e in locations.entries():
        out.append(LocationInfo(id=e["id"], kind="share", label=e["path"], path=e["path"], added=e["added"],
                                has_account=e["has_account"]))
    return out


def _location(loc_id: str) -> LocationInfo:
    hit = next((r for r in _roots() if r.id == loc_id), None)
    if hit is None:
        raise HTTPException(404, "허용된 위치가 아닙니다")
    return hit


def _target(loc: LocationInfo, rel: str) -> Path:
    """위치 기준 상대 경로 → 샌드박스를 거친 실제 경로 (허용 목록 밖·보호 구역이면 403)."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise HTTPException(403, "위치 밖으로 나갈 수 없습니다")
    raw = os.path.join(loc.path, *parts) if parts else loc.path
    try:
        path = _sandbox().resolve(raw)
    except SandboxError as e:
        raise HTTPException(403, str(e))
    if not locations.within(locations.norm(path), locations.norm(loc.path)):
        raise HTTPException(403, "위치 밖으로 나갈 수 없습니다")
    return path


def _io(loc: LocationInfo, fn: Any, where: str) -> Any:
    """네트워크 위치는 시간 제한 안에서. 실패는 갈래로 (A6)."""
    try:
        return locations.run_with_timeout(fn) if loc.kind == "share" else fn()
    except (OSError, TimeoutError) as e:
        code = 404 if isinstance(e, FileNotFoundError) else 504 if isinstance(e, TimeoutError) else 403
        raise HTTPException(code, locations.explain(e, where))


# ───────────────────────── 위치 목록 · 공유 위치 관리

@router.get("/locations", response_model=list[LocationInfo], summary="파일 탐색기 위치 목록 (작업 폴더 + 허용한 공유 위치)")
def list_locations() -> list[LocationInfo]:
    return _roots()


@router.post("/locations", response_model=LocationInfo, summary="공유 위치를 허용 목록에 더하기 (읽기 전용)")
def add_location(body: ShareAdd) -> LocationInfo:
    s = deps.load_settings()
    path = locations.to_path(body.path)
    if why := locations.refuse_reason(path, s):
        raise HTTPException(400, why)
    kind, err = locations.probe(path)
    if kind == "error":
        raise HTTPException(400, err)
    try:
        e = locations.add(locations.scope_for(path, kind), s)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    return next(r for r in _roots() if r.id == e["id"])


@router.delete("/locations/{loc_id}", response_model=Ok, summary="공유 위치를 허용 목록에서 지우기 (저장한 계정도 지운다)")
def remove_location(loc_id: str) -> Ok:
    if loc_id == "workspace":
        raise HTTPException(400, "작업 폴더는 지울 수 없습니다")
    if not locations.remove(loc_id):
        raise HTTPException(404, "허용된 위치가 아닙니다")
    return Ok()


@router.put("/locations/{loc_id}/account", response_model=Ok, summary="공유 위치의 별도 계정 저장 (값은 다시 돌려주지 않는다)")
def set_account(loc_id: str, body: ShareAccount) -> Ok:
    deps.require_admin_now()   # 공유 계정은 모두가 같이 쓰는 비밀 저장소에 들어간다
    if _location(loc_id).kind != "share":
        raise HTTPException(400, "공유 위치에만 계정을 넣을 수 있습니다")
    locations.set_account(loc_id, body.username, body.password)
    return Ok()


@router.delete("/locations/{loc_id}/account", response_model=Ok, summary="공유 위치의 별도 계정 지우기")
def clear_account(loc_id: str) -> Ok:
    deps.require_admin_now()
    _location(loc_id)
    locations.clear_account(loc_id)
    return Ok()


# ───────────────────────── 폴더 목록 · 파일 보기

@router.get("/locations/{loc_id}/list", response_model=DirListing, summary="폴더 목록 (나눠 받기·정렬·이름 찾기)")
def list_folder(loc_id: str, path: str = Query("", description="위치 기준 상대 경로"),
                offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=PAGE_MAX),
                sort: Literal["name", "size", "modified", "type"] = "name", order: Literal["asc", "desc"] = "asc",
                q: str = Query("", description="이름에 들어간 글자 (대소문자 무시)")) -> DirListing:
    loc = _location(loc_id)
    folder = _target(loc, path)

    def scan() -> list[DirEntry]:
        if not folder.is_dir():
            raise FileNotFoundError(str(folder))
        out = []
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith(".") and loc.kind == "workspace":
                    continue
                try:
                    st = e.stat()
                    is_dir = e.is_dir()
                except OSError:
                    continue
                out.append(DirEntry(name=e.name, dir=is_dir, size=0 if is_dir else st.st_size, modified=_when(st.st_mtime),
                                    type="" if is_dir else Path(e.name).suffix.lower().lstrip(".")))
        return out

    entries = _io(loc, scan, str(folder))
    if q:
        entries = [e for e in entries if q.lower() in e.name.lower()]
    keys = {"name": lambda e: e.name.lower(), "size": lambda e: e.size, "modified": lambda e: e.modified,
            "type": lambda e: (e.type, e.name.lower())}
    entries.sort(key=keys[sort], reverse=order == "desc")
    entries.sort(key=lambda e: not e.dir)   # 폴더 먼저 (정렬은 그 안에서 유지)
    rel = "/".join(p for p in path.replace("\\", "/").split("/") if p not in ("", "."))
    return DirListing(location=loc.id, path=rel, abs_path=str(folder), entries=entries[offset: offset + limit], total=len(entries),
                      folders=sum(e.dir for e in entries), files=sum(not e.dir for e in entries), offset=offset, limit=limit)


@router.get("/locations/{loc_id}/file", response_model=FilePreview, summary="파일 미리보기 (글자·코드·표·문서, 이미지는 크기만)")
def preview(loc_id: str, path: str = Query(..., description="위치 기준 상대 경로")) -> FilePreview:
    loc = _location(loc_id)
    target = _target(loc, path)

    def load() -> tuple[os.stat_result, documents.Document]:
        if not target.is_file():
            raise FileNotFoundError(str(target))
        return target.stat(), documents.read(target, limit=TEXT_LIMIT)

    st, doc = _io(loc, load, str(target))
    kind = doc.kind
    return FilePreview(location=loc.id, path=path, abs_path=str(target), name=target.name, size=st.st_size, modified=_when(st.st_mtime),
                       kind=kind, language=LANG.get(target.suffix.lower(), "text"),
                       text=doc.text if kind in ("text", "document", "table") else None, rows=doc.rows[:TABLE_ROWS],
                       sheets=doc.sheets, note=doc.note, width=doc.width, height=doc.height)


@router.get("/locations/{loc_id}/raw", include_in_schema=False)
def raw_image(loc_id: str, path: str = Query(...)) -> Response:
    """이미지 미리보기 (화면의 <img src>). 이미지 형식만, 큰 BMP 등은 줄여서. 주소에 인증 정보 없음."""
    loc = _location(loc_id)
    target = _target(loc, path)
    if target.suffix.lower() not in documents.IMAGE_EXT:
        raise HTTPException(404, "이미지가 아닙니다")

    def load() -> bytes:
        if target.stat().st_size > RAW_MAX:
            raise HTTPException(404, "이미지가 너무 커서 미리보기를 하지 않습니다")
        return target.read_bytes()

    data = _io(loc, load, str(target))
    fmt = imaging.sniff(data[:64])
    if not fmt:
        raise HTTPException(404, "이미지가 아닙니다")
    headers = {"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"}
    w, h = imaging.size_of(data, fmt) or (0, 0)
    if fmt not in imaging.BROWSER or fmt == "bmp" or max(w, h) > VIEW_SIDE:
        if (png := imaging.shrink_to_png(data, fmt, VIEW_SIDE)) is not None:
            return Response(png, media_type="image/png", headers=headers)
        if fmt not in imaging.BROWSER:
            raise HTTPException(404, "이 형식은 미리보기를 할 수 없습니다")
    return Response(data, media_type=imaging.RASTER[fmt], headers=headers)


