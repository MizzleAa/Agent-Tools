"""프롬프트 조각: 블록 · 루프 메시지 · 도구 설명 · 변수. 바꾼 것만 config/prompts.yaml 에 저장한다.

PUT 은 저장(기본값과 같아지면 저장하지 않음), DELETE 는 기본값으로 (직접 만든 블록·변수면 삭제).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from agent import history, prompts
from server import deps
from server.api.common import ERRORS, Ok, bad_request
from server.api.settings import schemas as sc
from server.api.settings.views import block_users

router = APIRouter(responses=ERRORS)
LABEL = {"blocks": "블록", "messages": "메시지", "tools": "도구 설명", "variables": "변수"}


def _change(section: str, key: str, value: Any) -> Ok:
    try:
        data = prompts.apply_change(prompts.read_user(), section, key, value)
    except ValueError as e:
        raise bad_request(e)
    note = f"{LABEL[section]} {'기본값으로' if value is None else '저장'} {key}"
    history.write(prompts.USER_FILE, prompts.user_text(data), deps.load_settings().data_path, note=note)
    return Ok()


@router.put("/blocks/{block_id}", response_model=Ok, summary="블록 저장 (없으면 새로 만든다)")
def put_block(block_id: str, body: sc.BlockWrite) -> Ok:
    return _change("blocks", block_id, body.model_dump())


@router.delete("/blocks/{block_id}", response_model=Ok, summary="블록 기본값으로 (직접 만든 블록이면 삭제)")
def delete_block(block_id: str) -> Ok:
    current = prompts.load_prompts().blocks.get(block_id)
    users = block_users(block_id)
    if current is not None and current.default is None and users:
        raise HTTPException(400, f"이 블록을 쓰는 프로필이 있습니다: {', '.join(users)}. 그 프로필의 구성에서 먼저 빼세요")
    return _change("blocks", block_id, None)


@router.put("/messages/{key}", response_model=Ok, summary="루프 메시지 저장")
def put_message(key: str, body: sc.TextWrite) -> Ok:
    return _change("messages", key, body.text)


@router.delete("/messages/{key}", response_model=Ok, summary="루프 메시지 기본값으로")
def delete_message(key: str) -> Ok:
    return _change("messages", key, None)


@router.put("/tools/{name}", response_model=Ok, summary="도구 설명 저장 (비우면 코드의 설명)")
def put_tool(name: str, body: sc.TextWrite) -> Ok:
    return _change("tools", name, body.text)


@router.delete("/tools/{name}", response_model=Ok, summary="도구 설명 기본값으로")
def delete_tool(name: str) -> Ok:
    return _change("tools", name, None)


@router.put("/variables/{name}", response_model=Ok, summary="전역 변수 저장 (없으면 새로 만든다)")
def put_variable(name: str, body: sc.VariableWrite) -> Ok:
    return _change("variables", name, body.model_dump())


@router.delete("/variables/{name}", response_model=Ok, summary="변수 기본값으로 (직접 만든 변수면 삭제)")
def delete_variable(name: str) -> Ok:
    return _change("variables", name, None)
