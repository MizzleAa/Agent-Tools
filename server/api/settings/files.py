"""파일로 관리하는 설정: 실행 파라미터(settings.yaml, 주석 유지) · 개발 규격(config/standards/*.md) · 변경 기록."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from agent import config, history, settings_file, standards
from server import deps
from server.api.common import ERRORS, Ok, bad_request
from server.api.settings import schemas as sc
from server.api.settings.views import settings_text

router = APIRouter(responses=ERRORS)


# ───────────────────────── 실행 파라미터

@router.put("/params/{section}", response_model=sc.ParamsSaved, summary="settings.yaml 의 한 구역 값 바꾸기 (주석 유지)")
def put_params(section: str, body: sc.ParamsWrite) -> sc.ParamsSaved:
    if section not in settings_file.PARAMS:
        raise HTTPException(404, f"고칠 수 없는 구역입니다: {section}")
    try:
        new = settings_file.update(settings_text(), section, body.values)
    except (ValueError, ValidationError) as e:
        raise bad_request(e)
    history.write(config.CONFIG_DIR / "settings.yaml", new, deps.load_settings().data_path,
                  note=f"실행 파라미터 {settings_file.PARAMS[section]['title']}")
    return sc.ParamsSaved(file=settings_file.file_values(new)[section])


# ───────────────────────── 개발 규격

def _standard(name: str) -> Path:
    path = standards.STANDARDS_DIR / name
    if not re.fullmatch(r"[^/\\]+\.md", name) or not path.exists():
        raise HTTPException(404, "규격 문서가 없습니다")
    return path


@router.get("/standards/{name}", response_model=sc.StandardText, summary="규격 문서 읽기")
def get_standard(name: str) -> sc.StandardText:
    return sc.StandardText(name=name, text=_standard(name).read_text(encoding="utf-8"))


@router.put("/standards/{name}", response_model=Ok, summary="규격 문서 저장")
def put_standard(name: str, body: sc.TextWrite) -> Ok:
    path = _standard(name)
    if not body.text.strip():
        raise HTTPException(400, "내용이 비어 있습니다")
    history.write(path, body.text, deps.load_settings().data_path, note=f"개발 규격 {name}")
    return Ok()


# ───────────────────────── 변경 기록

@router.get("/history", response_model=list[sc.HistoryEntry], summary="변경 기록 (최근 것부터)")
def list_history() -> list[dict]:
    return history.entries(deps.load_settings().data_path)


@router.get("/history/{entry_id}", response_model=sc.HistoryDetail, summary="기록 하나 (그때 내용 · 지금 내용 · 차이)")
def get_history(entry_id: str) -> dict:
    try:
        return history.get(deps.load_settings().data_path, entry_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/history/{entry_id}/restore", response_model=sc.RestoreResult, summary="그 저장 직전 상태로 되돌리기")
def restore_history(entry_id: str) -> sc.RestoreResult:
    try:
        return sc.RestoreResult(target=history.restore(deps.load_settings().data_path, entry_id))
    except ValueError as e:
        raise HTTPException(400, str(e))
