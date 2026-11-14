"""첨부 파일 보관.

화면에서 올린 파일은 앱 데이터 폴더 uploads/<세션 id>/ 에 저장한다 (프로젝트 폴더를 어지럽히지 않음).
이 폴더는 CLI 를 띄울 때 --add-dir 로 넘겨 Claude 가 Read 도구로 열 수 있다.
이미지는 메시지에 이미지 블록으로 직접 넣는다.
"""
from __future__ import annotations

import mimetypes
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024

_SAFE = re.compile(r"[^\w.\-가-힣 ()\[\]]+")


class Attachments:
    def __init__(self, root: Path):
        self.root = root

    def dir_for(self, session_id: str) -> Path:
        d = self.root / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, session_id: str, filename: str, data: bytes, content_type: str | None) -> dict[str, Any]:
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"파일이 너무 큽니다 (최대 {MAX_FILE_BYTES // 1024 // 1024}MB).")
        name = _SAFE.sub("_", Path(filename or "file").name).strip() or "file"
        att_id = uuid.uuid4().hex[:12]
        path = self.dir_for(session_id) / f"{att_id}__{name}"
        path.write_bytes(data)
        return self._describe(path, content_type)

    def _describe(self, path: Path, content_type: str | None = None) -> dict[str, Any]:
        att_id, _, name = path.name.partition("__")
        mime = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        if mime == "image/jpg":
            mime = "image/jpeg"
        size = path.stat().st_size
        kind = "image" if mime in IMAGE_TYPES and size <= MAX_IMAGE_BYTES else "file"
        return {"id": att_id, "name": name, "size": size, "mime": mime, "kind": kind, "path": str(path)}

    def resolve(self, session_id: str, ids: list[str]) -> list[dict[str, Any]]:
        out = []
        d = self.root / session_id
        for att_id in ids:
            if not re.fullmatch(r"[0-9a-f]{12}", att_id or ""):
                continue
            hits = list(d.glob(f"{att_id}__*")) if d.is_dir() else []
            if hits:
                out.append(self._describe(hits[0]))
        return out

    def remove_session(self, session_id: str) -> None:
        shutil.rmtree(self.root / session_id, ignore_errors=True)
