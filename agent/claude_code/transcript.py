"""대화 메시지 정규화 + 기록 읽기.

기록(이전 대화)은 Agent SDK 의 공식 함수 get_session_messages / get_session_info 로 읽기만 한다.
기록 파일을 고치거나, 읽은 내용으로 새 세션을 만들지 않는다.

기록에서 읽은 메시지(원시 API dict)와 실시간 SDK 메시지(dataclass)를 같은 모양으로 맞춘다:
    {uuid, role, kind, blocks:[...], model, parent_tool_use_id}
kind: prompt(사용자 입력) · assistant · tool_results(도구 결과만 담은 user 메시지) · notice(중단 표시 등) · meta(슬래시 명령 출력 등)
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, is_dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    get_session_info,
    get_session_messages,
)

MAX_TOOL_RESULT_CHARS = 30_000
MAX_INLINE_IMAGE_B64 = 4_000_000

_META_PREFIXES = ("<command-", "<local-command-", "<bash-", "<user-prompt-submit-hook", "<system-reminder", "<task-notification")


def _truncate(s: str, n: int = MAX_TOOL_RESULT_CHARS) -> tuple[str, bool]:
    if len(s) <= n:
        return s, False
    return s[:n], True


def _image_block(source: dict[str, Any] | None) -> dict[str, Any]:
    source = source or {}
    data = source.get("data") if source.get("type") == "base64" else None
    out: dict[str, Any] = {"type": "image", "media_type": source.get("media_type", "image/png")}
    if data and len(data) <= MAX_INLINE_IMAGE_B64:
        out["data"] = data
    else:
        out["omitted"] = True
    return out


def _flatten_result_content(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """도구 결과 content(str 또는 블록 목록)를 글자 + 이미지 목록으로."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                texts.append(str(b))
            elif b.get("type") == "text":
                texts.append(b.get("text", ""))
            elif b.get("type") == "image":
                images.append(_image_block(b.get("source")))
            elif b.get("type") == "tool_reference":
                texts.append(f"[tool: {b.get('tool_name', '')}]")
            else:
                texts.append(json.dumps(b, ensure_ascii=False)[:2000])
    else:
        texts.append(str(content))
    return "\n".join(texts), images


def _norm_block(b: dict[str, Any]) -> dict[str, Any] | None:
    t = b.get("type")
    if t == "text":
        return {"type": "text", "text": b.get("text", "")}
    if t in ("thinking", "redacted_thinking"):
        return {"type": "thinking", "text": b.get("thinking", "") or ""}
    if t in ("tool_use", "server_tool_use"):
        return {"type": "tool_use", "id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}}
    if isinstance(t, str) and (t == "tool_result" or t.endswith("_tool_result")):
        text, images = _flatten_result_content(b.get("content"))
        text, cut = _truncate(text)
        out = {"type": "tool_result", "tool_use_id": b.get("tool_use_id"), "is_error": bool(b.get("is_error")),
               "content": text, "truncated": cut}
        if images:
            out["images"] = images
        return out
    if t == "image":
        return _image_block(b.get("source"))
    if t == "document":
        src = b.get("source") or {}
        return {"type": "other", "label": f"문서 첨부 ({src.get('media_type', 'document')})"}
    return {"type": "other", "label": str(t)}


def _classify_user(blocks: list[dict[str, Any]]) -> str:
    if blocks and all(b["type"] == "tool_result" for b in blocks):
        return "tool_results"
    texts = [b.get("text", "") for b in blocks if b["type"] == "text"]
    if texts and all(t.startswith("[Request interrupted by user") for t in texts):
        return "notice"
    if texts and all(t.lstrip().startswith(_META_PREFIXES) for t in texts) and not any(b["type"] == "image" for b in blocks):
        return "meta"
    return "prompt"


def normalize_raw(uuid: str | None, msg_type: str, message: dict[str, Any], parent_tool_use_id: str | None = None) -> dict[str, Any]:
    """기록의 원시 API 메시지 → 정규화."""
    content = message.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    else:
        blocks = [nb for b in (content or []) if isinstance(b, dict) and (nb := _norm_block(b)) is not None]
    role = "assistant" if msg_type == "assistant" else "user"
    kind = "assistant" if role == "assistant" else _classify_user(blocks)
    return {
        "uuid": uuid,
        "role": role,
        "kind": kind,
        "blocks": blocks,
        "model": message.get("model") if role == "assistant" else None,
        "message_id": message.get("id") if role == "assistant" else None,
        "parent_tool_use_id": parent_tool_use_id,
    }


def _sdk_block_to_dict(b: Any) -> dict[str, Any]:
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ThinkingBlock):
        return {"type": "thinking", "thinking": b.thinking}
    if isinstance(b, ToolUseBlock):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    if isinstance(b, ToolResultBlock):
        return {"type": "tool_result", "tool_use_id": b.tool_use_id, "content": b.content, "is_error": b.is_error}
    if is_dataclass(b):
        d = asdict(b)
        d.setdefault("type", type(b).__name__)
        return d
    if isinstance(b, dict):
        return b
    return {"type": "other", "text": str(b)}


def normalize_sdk(msg: AssistantMessage | UserMessage) -> dict[str, Any]:
    """실시간 SDK 메시지 → 정규화 (기록과 같은 모양)."""
    if isinstance(msg, AssistantMessage):
        raw = {"role": "assistant", "content": [_sdk_block_to_dict(b) for b in msg.content], "model": msg.model,
               "id": msg.message_id}
        out = normalize_raw(msg.uuid, "assistant", raw, msg.parent_tool_use_id)
        if msg.error:
            out["error"] = str(msg.error)
        return out
    content = msg.content if isinstance(msg.content, str) else [_sdk_block_to_dict(b) for b in msg.content]
    return normalize_raw(msg.uuid, "user", {"role": "user", "content": content}, msg.parent_tool_use_id)


class HistoryReader:
    """세션 기록을 읽어 정규화한 결과를 (크기·수정시각) 기준으로 캐시한다."""

    def __init__(self, max_entries: int = 16):
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[tuple[int, int | None], str | None, list[dict[str, Any]]]] = {}
        self._max = max_entries

    def info(self, claude_session_id: str, directory: str | None) -> Any:
        info = get_session_info(claude_session_id, directory=directory) if directory else None
        if info is None:
            info = get_session_info(claude_session_id)
        return info

    def exists(self, claude_session_id: str, directory: str | None) -> bool:
        try:
            return self.info(claude_session_id, directory) is not None
        except Exception:  # noqa: BLE001
            return False

    def read(self, claude_session_id: str, directory: str | None) -> list[dict[str, Any]]:
        info = self.info(claude_session_id, directory)
        if info is None:
            return []
        key = (info.last_modified, info.file_size)
        with self._lock:
            hit = self._cache.get(claude_session_id)
            if hit and hit[0] == key:
                return hit[2]
        msgs = get_session_messages(claude_session_id, directory=info.cwd or directory)
        if not msgs and directory:
            msgs = get_session_messages(claude_session_id)
        norm = [normalize_raw(m.uuid, m.type, m.message or {}, m.parent_tool_use_id) for m in msgs]
        with self._lock:
            self._cache[claude_session_id] = (key, info.cwd, norm)
            while len(self._cache) > self._max:
                self._cache.pop(next(iter(self._cache)))
        return norm

    def page(self, claude_session_id: str, directory: str | None, limit: int, before: int | None) -> dict[str, Any]:
        msgs = self.read(claude_session_id, directory)
        total = len(msgs)
        end = total if before is None else max(0, min(before, total))
        start = max(0, end - limit)
        return {"total": total, "start": start, "end": end, "messages": msgs[start:end]}
