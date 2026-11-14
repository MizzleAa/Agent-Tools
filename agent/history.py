"""설정 파일 변경 기록. 관리 화면에서 저장할 때마다 바뀌기 전 내용을 data/settings_history/ 에 남긴다.

'되돌리기'는 그 기록의 내용을 원래 파일에 다시 쓰는 것이다 (되돌리기 직전 내용도 기록에 남는다).
기록할 수 있는 파일은 config 폴더의 프롬프트·프로필·규격·settings.yaml·connections.yaml 로 한정한다.
"""

from __future__ import annotations

import difflib
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agent import config

KEEP = 300   # 최근 몇 개까지 남길지
_ALLOWED = re.compile(r"^(prompts\.yaml|settings\.yaml|connections\.yaml|agents/[a-z0-9][a-z0-9_-]{0,31}\.yaml|workflows/[a-z0-9][a-z0-9_-]{0,31}\.yaml|standards/[^/\\]+\.md)$")


def _dir(data_path: Path) -> Path:
    return data_path / "settings_history"


def rel_target(path: Path) -> str:
    """config 폴더 기준 경로. 허용된 파일이 아니면 ValueError."""
    rel = Path(path).resolve().relative_to(config.CONFIG_DIR.resolve()).as_posix()
    if not _ALLOWED.match(rel):
        raise ValueError(f"기록·복원할 수 없는 파일입니다: {rel}")
    return rel


def write(path: Path, text: str | None, data_path: Path, note: str = "") -> str:
    """파일을 새 내용으로 바꾼다 (text=None 이면 지운다). 바꾸기 전 내용을 기록으로 남기고 기록 id 를 돌려준다."""
    target = rel_target(path)
    raw = path.read_bytes().decode("utf-8", errors="replace") if path.exists() else None
    old = raw.replace("\r\n", "\n") if raw is not None else None
    if text is not None:
        text = text.replace("\r\n", "\n")
    if old == text:
        return ""
    entry_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f-") + uuid.uuid4().hex[:4]   # 이름순 = 시간순 (같은 초 안에서도)
    folder = _dir(data_path)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{entry_id}.json").write_text(json.dumps(
        {"id": entry_id, "target": target, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note,
         "existed": old is not None, "text": old or ""}, ensure_ascii=False), encoding="utf-8")
    if text is None:
        path.unlink(missing_ok=True)
    else:   # 원래 파일의 줄 끝(LF/CRLF)을 그대로 쓴다 (diff 가 파일 전체로 번지지 않게). 새 파일은 LF
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((text.replace("\n", "\r\n") if raw and "\r\n" in raw else text).encode("utf-8"))
    for stale in sorted(folder.glob("*.json"))[:-KEEP]:
        stale.unlink(missing_ok=True)
    return entry_id


def entries(data_path: Path, limit: int = 100) -> list[dict[str, Any]]:
    out = []
    for p in sorted(_dir(data_path).glob("*.json"), reverse=True)[:limit]:
        try:
            e = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({k: e[k] for k in ("id", "target", "time", "note", "existed")})
    return out


def get(data_path: Path, entry_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[\w-]+", entry_id):
        raise ValueError("잘못된 기록 id")
    p = _dir(data_path) / f"{entry_id}.json"
    if not p.exists():
        raise ValueError("기록이 없습니다")
    e = json.loads(p.read_text(encoding="utf-8"))
    rel_target(config.CONFIG_DIR / e["target"])   # 기록 파일이 조작됐어도 config 밖은 건드리지 않는다
    current_path = config.CONFIG_DIR / e["target"]
    current = current_path.read_text(encoding="utf-8") if current_path.exists() else ""
    e["current"] = current
    e["diff"] = "".join(difflib.unified_diff(
        e["text"].splitlines(keepends=True), current.splitlines(keepends=True),
        fromfile=f"그때 ({e['time']})", tofile="지금", n=2))
    return e


def restore(data_path: Path, entry_id: str) -> str:
    """그 기록의 내용(그 저장 직전 상태)으로 파일을 되돌린다. 되돌린 파일의 상대 경로를 돌려준다."""
    e = get(data_path, entry_id)
    path = config.CONFIG_DIR / e["target"]
    write(path, e["text"] if e["existed"] else None, data_path, note=f"되돌리기 ({e['time']} 기록)")
    return e["target"]
