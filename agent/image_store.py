"""화면에 붙일 이미지를 실행 폴더에 받아 두고 기록한다 (기능 요구 '이미지 표시').

api_get 이 이미지를 돌려주는 경로를 받았거나 show_images 를 부르면 여기로 온다.
- 원본은 data/runs/<실행>/images/<번호>.orig.<형식>, 썸네일은 <번호>.thumb.png (큰 BMP 등은 줄여 둔다).
- 기록(번호·이름·경로·성공/실패와 이유·크기)은 ctx.memo 에 쌓이고, 루프가 Images 이벤트로 화면에 보낸다.
- 모델에게는 한 줄 설명만 돌려준다. 이미지 바이트는 모델 입력·이벤트·실행 기록에 넣지 않는다.
- 인증 정보는 다루지 않는다 (이미지는 API 도구가 서버 쪽 헤더로 받아 온 바이트만 받는다).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from agent import grounding, imaging

MAX_PER_RUN = 48             # 한 실행에서 화면에 붙이는 최대 장수 (화면·서버 부담)
MAX_IMAGE_BYTES = 60 * 2**20
THUMB_SIDE = 320             # 썸네일 긴 변
VIEW_SIDE = 1600             # 크게 보기 긴 변 (원본이 크면 이만큼 줄인 PNG — 화면이 처음 열 때 만든다)
SMALL_ORIGINAL = 400 * 1024  # 이보다 작은 PNG·JPEG 등은 원본을 썸네일로 써도 가볍다
# 문서 요약에 이런 낱말이 있는 조회 경로는 '이미지를 돌려주는 경로'로 본다 (실패하면 화면에 '찾을 수 없음' 칸을 그린다)
IMAGE_SUMMARY = re.compile(r"\b(image|images|picture|pictures|photo|photos|thumbnail|thumbnails|thumb|preview|original|"
                           r"bitmap|jpe?g|png|bmp|gif|webp|tiff?|media)\b", re.I)
_GENERIC = {"detail", "details", "preview", "thumbnail", "thumb", "original", "raw", "image", "images", "file", "files",
            "download", "view", "content", "data", "media", "bytes", "src", "source", "picture", "photo", "icon"}


def image_dir(run_dir: Path) -> Path:
    return run_dir / "images"


def label_of(path: str, memo: dict[str, Any] | None = None) -> str:
    """경로에서 사람이 알아볼 이름. 경로의 ID 를 앞서 받은 결과가 이름(파일명 등)과 함께 알려 줬으면 그 이름 (QA IMG-3:
    ID 경로로 부르면 라벨이 UUID 라 답변의 파일명과 맞춰 보기 어려웠다). 아니면 뒤에서부터 식별자인 조각, 일반 낱말이 아닌 조각."""
    segs = [unquote(s) for s in path.split("?")[0].split("/") if s]
    known = (memo or {}).get("id_names") or {}
    # 뒤에서부터 처음 나오는 '대상을 가리키는' 조각이 그 이미지다 (앞쪽의 앨범·데이터셋 ID 가 아니라)
    named = next((s for s in reversed(segs) if s.lower() in known or grounding.is_identifier(s)
                  or re.search(r"\.[A-Za-z]\w{1,4}$", s)), "")
    if named:
        return known.get(named.lower(), named)
    return next((s for s in reversed(segs) if s.lower() not in _GENERIC), path)


_NAME_FIELDS = ("file_name", "filename", "name", "display_name", "title", "label")
_OWN_ID = ("id", "uuid", "guid")


def remember_names(memo: dict[str, Any], data: Any, limit: int = 50_000) -> None:
    """받은 JSON 에서 '이 객체의 ID → 이 객체의 이름' 을 기억한다 (이미지 라벨용). 다른 객체를 가리키는 owner_id 같은 참조는 쓰지
    않는다 — 객체 자신의 id/uuid, 그게 없으면 하나뿐인 …_id 값만."""
    names: dict[str, str] = memo.setdefault("id_names", {})

    def walk(x: Any, depth: int) -> None:
        if depth > 8 or len(names) >= limit:
            return
        if isinstance(x, list):
            for v in x:
                walk(v, depth + 1)
            return
        if not isinstance(x, dict):
            return
        name = next((x[k] for k in _NAME_FIELDS if isinstance(x.get(k), str) and x[k].strip()), "")
        if name:
            own = [x[k] for k in _OWN_ID if isinstance(x.get(k), str)]
            if not own:
                refs = [v for k, v in x.items() if k.lower().endswith("_id") and isinstance(v, str) and grounding.is_identifier(v)]
                own = refs if len(refs) == 1 else []
            for v in own:
                if grounding.is_identifier(v) and v != name:
                    names.setdefault(v.lower(), name)
        for v in x.values():
            if isinstance(v, (dict, list)):
                walk(v, depth + 1)

    walk(data, 0)


def image_paths(docs: str, k: int = 8) -> list[str]:
    """문서에서 이미지 자체를 돌려주는 조회 줄 (QA IMG-2). 이름(파일명)으로 받는 경로를 먼저, 가벼운 미리보기·썸네일을 먼저."""
    found = [line for line in docs.splitlines() if _is_image_line(line)]

    def rank(line: str) -> tuple[bool, bool, int]:
        path = line.split()[1].split("?")[0]
        by_name = any(re.search(r"name|file", s, re.I) for s in re.findall(r"\{([^}]*)\}", path))
        return not by_name, not re.search(r"thumb|preview", line, re.I), len(path)

    return sorted(found, key=rank)[:k]


def _template(path: str) -> re.Pattern:
    parts = re.split(r"\{[^}]*\}", path.rstrip("/"))
    return re.compile("^" + "[^/]+".join(map(re.escape, parts)) + "/?$")


def is_image_endpoint(docs: str, path: str) -> bool:
    """문서에서 이 경로와 맞는 조회 줄의 요약이 이미지 자체를 돌려준다고 하는지. 이미지에 '대한' 정보(메타·상세·목록·검색·
    통계·이력)를 돌려주는 줄은 아니다. 받지 못했을 때 화면에 '찾을 수 없음' 칸을 그릴지만 정한다 — 성공은 받은 바이트로 정한다."""
    from agent import openapi
    base = path.split("?")[0]
    for line in docs.splitlines():
        m = openapi.ENDPOINT_LINE.match(line)
        if m and m.group(1) == "GET" and _template(m.group(2).split("?")[0]).match(base) and _is_image_line(line):
            return True
    return False


# 이미지에 '대한' 정보를 돌려주는 줄: 요약에 이런 낱말이 있거나, 경로의 마지막 고정 조각이 이런 이름이다
_ABOUT = re.compile(r"\b(meta|metadata|details?|list|lists|search|paged|filter|count|counts|stats?|statistics|summary|"
                    r"annotations?|history|info|json|attributes?|export|zip|archive)\b", re.I)
_ABOUT_PATH = re.compile(r"^(detail|details|meta|metadata|history|neighbors?|stats?|statistics|summary|count|counts|"
                         r"provenance|datasets?|labels?|annotations?|attr-keys|attributes|export|locks?|reviews?|status|"
                         r"search|names|files)$", re.I)


def _is_image_line(line: str) -> bool:
    """문서 한 줄이 이미지 자체를 돌려주는 조회 경로인지 (요약의 낱말 + 경로의 마지막 고정 조각으로)."""
    from agent import openapi
    m = openapi.ENDPOINT_LINE.match(line)
    if not m or m.group(1) != "GET":
        return False
    summary = line[m.end():]
    literal = [s for s in m.group(2).split("?")[0].strip("/").split("/") if s and not s.startswith("{")]
    last = literal[-1] if literal else ""
    return bool(IMAGE_SUMMARY.search(summary) or IMAGE_SUMMARY.fullmatch(last)) and not _ABOUT.search(summary) \
        and not _ABOUT_PATH.match(last)


def room(memo: dict[str, Any]) -> int:
    return MAX_PER_RUN - len(memo.get("images", []))


def record(memo: dict[str, Any], run_dir: Path, api: str, path: str, *, ok: bool, reason: str = "",
           data: bytes = b"", fmt: str = "", label: str = "") -> dict[str, Any]:
    """한 장을 기록한다 (성공이면 원본·썸네일을 저장). 화면에 붙일 수 없으면 ok=False 와 이유."""
    shown: list[dict[str, Any]] = memo.setdefault("images", [])
    n = len(shown) + 1
    entry: dict[str, Any] = {"id": n, "api": api, "path": path, "label": label or label_of(path, memo), "ok": False,
                             "reason": reason, "format": fmt, "width": 0, "height": 0, "bytes": len(data)}
    if ok:
        thumb = None
        if len(data) > MAX_IMAGE_BYTES:
            entry["reason"] = f"너무 큼 ({len(data) // 2**20}MB)"
        else:
            if fmt == "bmp" or fmt not in imaging.BROWSER or len(data) > SMALL_ORIGINAL:   # BMP 는 압축이 없어 늘 줄인다
                thumb = imaging.shrink_to_png(data, fmt, THUMB_SIDE)
            if fmt not in imaging.BROWSER and thumb is None:   # 브라우저가 못 그리고 바꿀 수도 없는 형식 (TIFF 등, Pillow 없음)
                entry["reason"] = f"화면에 그릴 수 없는 형식 ({fmt})"
            else:
                folder = image_dir(run_dir)
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{n}.orig.{fmt}").write_bytes(data)
                if thumb is not None:
                    (folder / f"{n}.thumb.png").write_bytes(thumb)
                else:   # 작은 PNG·JPEG 등: 원본을 그대로 썸네일로 — 보관 기간이 지나 원본을 지워도 대화에 남게
                    (folder / f"{n}.small.{fmt}").write_bytes(data)
                w, h = imaging.size_of(data, fmt) or (0, 0)
                entry.update(ok=True, reason="", width=w, height=h)
    shown.append(entry)
    memo.setdefault("images_new", []).append(entry)
    return entry


def prune(data_path: Path, keep_days: int) -> int:
    """보관 기간이 지난 원본·크게 보기 사본을 지운다 (사용자 결정: 원본은 N일 뒤 삭제, 썸네일은 대화를 지울 때까지). 지운 개수."""
    import time
    cutoff = time.time() - max(1, keep_days) * 86400
    removed = 0
    for folder in (data_path / "runs").glob("*/images"):
        for f in list(folder.glob("*.orig.*")) + list(folder.glob("*.view.png")):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


def line_for(entry: dict[str, Any]) -> str:
    """모델에게 돌려줄 한 줄 (이미지 내용은 없다)."""
    if entry["ok"]:
        return (f"🖼 이미지 #{entry['id']} '{entry['label']}' ({entry['format']} {entry['width']}×{entry['height']}, "
                f"{max(1, entry['bytes'] // 1024)}KB) 를 채팅 화면에 붙였습니다. 이미지 내용은 볼 수 없습니다 — 답에는 붙인 이미지의 이름만 쓰세요.")
    return f"🖼 이미지 #{entry['id']} '{entry['label']}' 는 화면에 '이미지를 찾을 수 없음'으로 표시됩니다 ({entry['reason']})."
