"""모델 목록: GGUF 추가 · 모델별 값(컨텍스트·GPU 층·추가 인자) · 빼기 · 기본값으로. 저장은 config/connections.yaml.

모델 교체(고르기)와 '다시 켜서 적용'은 PUT /models/active (system).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import ValidationError

from agent import config, history, model_presets
from server import deps
from server.api.common import ERRORS, Ok, bad_request
from server.api.settings import schemas as sc

router = APIRouter(responses=ERRORS)


def _save(data: dict, note: str) -> None:
    history.write(config.CONNECTIONS_FILE, model_presets.write_text(data), deps.load_settings().data_path, note=note)


def _saved(name: str, restart: bool) -> sc.ModelSaved:
    info = next(m for m in model_presets.payload(deps.load_settings()) if m["name"] == name)
    return sc.ModelSaved(model=sc.ModelPresetInfo(**info), restart_needed=restart)


@router.post("/models", response_model=sc.ModelSaved, status_code=201, summary="모델 목록에 GGUF 추가")
def create_model(body: sc.ModelCreate) -> sc.ModelSaved:
    try:
        data = model_presets.add(deps.load_settings(), model_presets.read(), body.model_dump())
    except (ValueError, ValidationError) as e:
        raise bad_request(e)
    _save(data, f"모델 추가 {body.name}")
    return _saved(body.name, False)


@router.put("/models/{name}", response_model=sc.ModelSaved, summary="모델별 값 바꾸기")
def update_model(name: str, body: sc.ModelUpdate) -> sc.ModelSaved:
    s = deps.load_settings()
    try:
        data = model_presets.update(s, model_presets.read(), name, body.model_dump(exclude_unset=True))
    except (ValueError, ValidationError) as e:
        raise bad_request(e)
    _save(data, f"모델 설정 {name}")
    return _saved(name, name == deps.load_settings().backend.model)


@router.delete("/models/{name}", response_model=Ok, summary="목록에서 빼기 (화면에서 추가한 모델만, GGUF 파일은 그대로)")
def delete_model(name: str) -> Ok:
    try:
        data = model_presets.remove(deps.load_settings(), model_presets.read(), name)
    except ValueError as e:
        raise bad_request(e)
    _save(data, f"모델 삭제 {name}")
    return Ok()


@router.post("/models/{name}/reset", response_model=sc.RestartNeeded, summary="settings.yaml 에 적힌 값으로")
def reset_model(name: str) -> sc.RestartNeeded:
    _save(model_presets.reset(model_presets.read(), name), f"모델 기본값으로 {name}")
    return sc.RestartNeeded(restart_needed=name == deps.load_settings().backend.model)
