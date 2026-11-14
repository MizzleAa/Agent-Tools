"""Claude(Anthropic) 모델 호출 — 공식 anthropic SDK.

에이전트 루프(agent/loop.py)는 OpenAI 모양의 대화(system · user · assistant(tool_calls) · tool)를 쓴다.
이 파일은 그 대화를 Messages API 모양으로 바꿔 보내고, 응답을 LLMResponse 로 돌려준다 (LLMClient 와 같은 인터페이스).

- 추론(thinking): 적응형(adaptive)을 쓰는 모델은 받은 thinking 블록을 **그대로** 다음 요청에 돌려보낸다
  (LLMResponse.raw → assistant 메시지의 '_raw'). 컨텍스트 한도 때문에 앞 대화를 줄이면 블록이 맞지 않게 되므로
  prefix_mismatch_behavior=drop_block 으로 그 블록만 버리고 계속하게 한다 (오류로 멈추지 않게).
- 거절(refusal)에 대비해 지원 모델은 서버 쪽 대체 모델(fallbacks "default")을 켠다.
- 자동 프롬프트 캐시(cache_control)를 켜서 같은 앞부분(시스템 지침·도구·지난 대화)을 싸게 다시 읽는다.
- 오류는 OpenAI SDK 예외 모양으로 바꿔 던진다 (루프의 연결 실패·컨텍스트 초과 처리를 그대로 쓰려고).
"""

from __future__ import annotations

import json
import re
from typing import Any

import anthropic
import httpx2
import openai

from agent.config import Backend, Sampling
from agent.llm import LLMResponse, ToolCallRequest, leaked_call_syntax

DEFAULT_MAX_TOKENS = 16_000
THINKING_BETA = "thinking-binding-controls-2026-08-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_ADAPTIVE = re.compile(r"^claude-(?:(?:opus|sonnet)-(?:4-[6-9]|[5-9])|fable|mythos)")
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_NO_EFFORT = re.compile(r"^claude-(?:haiku-4-5|sonnet-4-5|3)")


class ProviderAPIError(openai.APIError):
    """외부 공급사 오류 (HTTP 상태를 지닌다 — providers.describe_error 가 사유를 나눈다)."""

    def __init__(self, message: str, request: Any, status_code: int | None):
        super().__init__(message, request, body=None)
        self.status_code = status_code


class AnthropicClient:
    def __init__(self, backend: Backend, sampling: Sampling, client: Any = None):
        self.backend = backend
        self.sampling = sampling
        default_url = backend.base_url.rstrip("/") in ("", "https://api.anthropic.com")
        if client is None:   # 계정 로그인(프로필)이면 공식 SDK 가 토큰을 읽고 갱신한다
            from agent.providers import anthropic_client
            client = anthropic_client(backend.api_key, backend.profile, backend.base_url, backend.timeout, 2)
        self.client = client
        self.official = default_url   # 서버 쪽 대체 모델(fallbacks)은 Anthropic 직접 연결에서만
        self._last_tools: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return self.backend.model

    # ───────────────────────── 요청

    def request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        system, converted = to_anthropic(messages)
        model = self.backend.model
        kwargs: dict[str, Any] = dict(model=model, max_tokens=self.sampling.max_tokens or DEFAULT_MAX_TOKENS,
                                      messages=converted, cache_control={"type": "ephemeral"})
        if system:
            kwargs["system"] = system
        if tools:
            self._last_tools = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                                 "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                                for t in tools]
            kwargs["tools"] = self._last_tools
        elif _has_tool_use(converted):
            # 루프가 '도구 없이 마무리 답'을 받을 때: 대화에 도구 호출이 있으면 도구 정의가 있어야 하므로 같은 목록을 보내고
            # tool_choice none 으로 막는다 (도구 목록을 바꾸지 않아야 앞의 추론 블록도 그대로 유효하다)
            kwargs["tools"] = self._last_tools or _tools_named_in(converted)
            kwargs["tool_choice"] = {"type": "none"}
        betas: list[str] = []
        if _ADAPTIVE.match(model):
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized",
                                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
            betas.append(THINKING_BETA)
        if self.backend.effort and not _NO_EFFORT.match(model):
            kwargs["output_config"] = {"effort": self.backend.effort}
        if self.official and model in _FALLBACK_MODELS:
            kwargs["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)
        if betas:
            kwargs["betas"] = betas
        return kwargs

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             think: bool | None = None) -> LLMResponse:
        # think: Qwen 의 '생각 끄기' 스위치. 적응형 추론 모델은 끌 수 없고(400) 스스로 정도를 정하므로 쓰지 않는다.
        kwargs = self.request(messages, tools)
        try:
            r = self.client.beta.messages.create(**kwargs)
        except anthropic.APIConnectionError as e:   # APITimeoutError 포함
            raise openai.APIConnectionError(message=f"{type(e).__name__}: {e}", request=_req(self.backend)) from e
        except anthropic.BadRequestError as e:
            text = str(getattr(e, "message", e))
            if "too long" in text.lower() or "context" in text.lower():   # 루프가 줄여서 다시 보낸다
                raise openai.BadRequestError(f"context too long: {text}", response=httpx2.Response(400, request=_req(self.backend)),
                                             body=None) from e
            raise ProviderAPIError(text, _req(self.backend), 400) from e
        except anthropic.APIStatusError as e:
            raise ProviderAPIError(str(getattr(e, "message", e)), _req(self.backend), e.status_code) from e
        return from_anthropic(r)


def _has_tool_use(messages: list[dict[str, Any]]) -> bool:
    return any(b.get("type") in ("tool_use", "tool_result") for m in messages for b in m["content"])


def _tools_named_in(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = sorted({b.get("name", "") for m in messages for b in m["content"] if b.get("type") == "tool_use"} - {""})
    return [{"name": n, "description": "", "input_schema": {"type": "object", "properties": {}}} for n in names]


def _req(backend: Backend) -> httpx2.Request:
    return httpx2.Request("POST", (backend.base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages")


# ───────────────────────── 대화 모양 바꾸기 (OpenAI → Anthropic)

def _text_of(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def to_anthropic(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """(system, messages). 첫 system 은 맨 위 지침으로, 중간 system 은 사용자 쪽 지침 글로 넣는다.
    연속된 tool 결과는 user 메시지 하나에 모은다 (병렬 도구 호출 결과는 한 번에 돌려줘야 한다)."""
    system = ""
    out: list[dict[str, Any]] = []

    def add(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "system":
            text = _text_of(m.get("content"))
            if i == 0:
                system = text
            elif text:
                add("user", [{"type": "text", "text": f"[지침]\n{text}"}])
        elif role == "user":
            text = _text_of(m.get("content"))
            add("user", [{"type": "text", "text": text or "(빈 메시지)"}])
        elif role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                          "content": _text_of(m.get("content")) or "(출력 없음)"}])
        elif role == "assistant":
            if m.get("_raw"):   # Claude 가 낸 블록 그대로 (thinking 서명 포함)
                add("assistant", [dict(b) for b in m["_raw"]])
                continue
            blocks: list[dict[str, Any]] = []
            if text := _text_of(m.get("content")).strip():
                blocks.append({"type": "text", "text": text})
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": c.get("id", ""), "name": fn.get("name", ""),
                               "input": args if isinstance(args, dict) else {}})
            add("assistant", blocks or [{"type": "text", "text": "(빈 답)"}])
    if out and out[0]["role"] != "user":   # 첫 메시지는 user 여야 한다
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(이전 대화에 이어서)"}]})
    return system, out


# ───────────────────────── 응답 읽기 (Anthropic → LLMResponse)

_STOP = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length",
         "model_context_window_exceeded": "length", "pause_turn": "stop", "refusal": "refusal"}


def _dump(block: Any) -> dict[str, Any]:
    return block.model_dump(mode="json", exclude_none=True) if hasattr(block, "model_dump") else dict(block)


def from_anthropic(r: Any) -> LLMResponse:
    blocks = [_dump(b) for b in r.content]
    # 대체 모델로 넘어간 지점(fallback 블록) 앞의 thinking · tool_use 는 다시 보내지 않는다 (문서의 '되돌려 보내기' 규칙)
    last_fb = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    raw = [b for i, b in enumerate(blocks)
           if not (i < last_fb and b.get("type") in ("thinking", "redacted_thinking", "tool_use"))]
    text, reasoning, calls = [], [], []
    for b in raw:
        kind = b.get("type")
        if kind == "text":
            text.append(b.get("text", ""))
        elif kind == "thinking":
            reasoning.append(b.get("thinking", ""))
        elif kind == "tool_use":
            args = b.get("input") if isinstance(b.get("input"), dict) else {}
            calls.append(ToolCallRequest(b.get("id", ""), b.get("name", ""), args, error=leaked_call_syntax(args)))
    content = "\n".join(t for t in text if t).strip()
    stop = getattr(r, "stop_reason", "") or ""
    if stop == "refusal":
        details = getattr(r, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        content = content or f"(모델이 이 요청을 거절했습니다{f' — 분류: {category}' if category else ''}. 요청을 바꾸거나 다른 모델을 고르세요.)"
    usage = getattr(r, "usage", None)
    prompt = sum(int(getattr(usage, k, 0) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                          "cache_creation_input_tokens")) if usage else 0
    return LLMResponse(content=content, reasoning="\n".join(t for t in reasoning if t).strip(), tool_calls=calls,
                       prompt_tokens=prompt, completion_tokens=int(getattr(usage, "output_tokens", 0) or 0) if usage else 0,
                       finish_reason=_STOP.get(stop, stop), raw=raw)
