"""OpenAI 호환 API 클라이언트 래퍼.

llama.cpp(llama-server), vLLM 같은 서버는 모두 /v1/chat/completions 를 제공하므로 서버를 바꿔도
이 파일만 맞으면 나머지 코드는 그대로다. 서버마다 다른 부분(thinking 출력 위치, 켜고 끄는
방법)은 여기서 하나로 정리한다.

외부 AI 모델(agent/providers.py): OpenAI 와 Gemini(OpenAI 호환 주소)도 이 클래스로 부른다 — 로컬 전용 파라미터
(enable_thinking·top_k·min_p)와 샘플링 값은 보내지 않고 공급사 기본값을 쓴다. Claude 는 agent/llm_anthropic.py.
make_client() 가 Backend.provider 를 보고 고른다.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import json_repair
from openai import OpenAI

from agent.config import Backend, Sampling

_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.S)


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any]
    error: str = ""  # 인자 JSON 을 끝내 해석하지 못했을 때의 이유
    extra: dict[str, Any] = field(default_factory=dict)   # 공급사가 되돌려 받아야 하는 값 (Gemini 의 사고 서명 extra_content)


@dataclass
class LLMResponse:
    content: str
    reasoning: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""
    raw: Any = None   # 공급사 모양 그대로의 답 (Claude 의 thinking 블록 — 다음 요청에 그대로 돌려보낸다)


class LLMClient:
    def __init__(self, backend: Backend, sampling: Sampling):
        self.backend = backend
        self.sampling = sampling
        self.client = OpenAI(base_url=backend.base_url, api_key=backend.api_key, timeout=backend.timeout, max_retries=2)

    @property
    def model(self) -> str:
        return self.backend.model

    def request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                think: bool | None = None) -> dict[str, Any]:
        if self.backend.external:
            return self._external_request(messages, tools)
        kwargs: dict[str, Any] = dict(
            model=self.backend.model,
            messages=_plain(messages),
            temperature=self.sampling.temperature,
            top_p=self.sampling.top_p,
        )
        if tools:
            kwargs["tools"] = tools
        for key in ("presence_penalty", "max_tokens"):   # OpenAI 표준 파라미터. 설정에 있을 때만 보낸다
            if getattr(self.sampling, key, None) is not None:
                kwargs[key] = getattr(self.sampling, key)
        extra = self._thinking_params(think)
        for key in ("top_k", "min_p"):   # llama-server·vLLM 전용 파라미터는 extra_body 로
            if getattr(self.sampling, key, None) is not None:
                extra[key] = getattr(self.sampling, key)
        if extra:
            kwargs["extra_body"] = extra
        return kwargs

    def _external_request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        """OpenAI · Gemini: 샘플링(temperature 등)은 보내지 않는다 — 추론 모델은 기본값 외의 값을 거절하고,
        Gemini 3 은 기본 온도(1.0)에서 벗어나면 같은 말을 되풀이하기 쉽다."""
        kwargs: dict[str, Any] = dict(model=self.backend.model, messages=_plain(messages, keep_extra=True))
        if tools:
            kwargs["tools"] = tools
        if self.sampling.max_tokens:
            kwargs["max_completion_tokens" if self.backend.provider == "openai" else "max_tokens"] = self.sampling.max_tokens
        if self.backend.effort:
            kwargs["reasoning_effort"] = self.backend.effort
        return kwargs

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             think: bool | None = None) -> LLMResponse:
        kwargs = self.request(messages, tools, think)

        r = self.client.chat.completions.create(**kwargs)
        choice = r.choices[0]
        msg = choice.message
        content = msg.content or ""
        extra_fields = msg.model_extra or {}
        # llama-server·vLLM 은 'reasoning_content', 일부 서버는 'reasoning', 설정에 따라 본문 속 <think>
        reasoning = extra_fields.get("reasoning_content") or extra_fields.get("reasoning") or ""
        for block in _THINK_BLOCK.findall(content):
            reasoning += block
        content = _THINK_BLOCK.sub("", content).strip()

        return LLMResponse(
            content=content,
            reasoning=reasoning.strip(),
            tool_calls=[_parse_tool_call(tc) for tc in (msg.tool_calls or [])],
            prompt_tokens=r.usage.prompt_tokens if r.usage else 0,
            completion_tokens=r.usage.completion_tokens if r.usage else 0,
            finish_reason=choice.finish_reason or "",
        )

    @staticmethod
    def _thinking_params(think: bool | None) -> dict[str, Any]:
        # Qwen3 채팅 템플릿의 enable_thinking 스위치. llama-server 와 vLLM 모두 이 방식으로 받는다.
        if think is None:
            return {}
        return {"chat_template_kwargs": {"enable_thinking": think}}


# 모델의 도구 호출 문법(<function=…><parameter=…>…</parameter>)이 JSON 인자 값 안에 섞여 들어오는 일이 있다
# (QA R09: path 값에 '\n</parameter><parameter=content>\n…'). 그대로 쓰면 엉뚱한 사유(경로에 ':' 금지)로 거부돼
# 모델이 원인을 모른다 → 형식이 깨졌다고 바로 알린다.
# Qwen 계열의 XML 식 호출 표시. 보통 HTML·XML 의 <function>·</parameter> 는 내용일 수 있으므로
# 이름이 붙은 여는 표시(<parameter=path>), tool_call 표시, 값 끝에 붙은 닫는 표시만 본다.
_LEAKED_SYNTAX = re.compile(r"<(?:parameter|function)=[\w.-]+>|</?tool_call>|</(?:parameter|function)>\s*$")


def leaked_call_syntax(arguments: dict[str, Any]) -> str:
    """인자 값에 도구 호출 문법이 섞였으면 모델에게 줄 설명, 아니면 빈 글자."""
    for key, value in arguments.items():
        if isinstance(value, str) and _LEAKED_SYNTAX.search(value):
            return (f"도구 호출 형식이 깨졌습니다: '{key}' 값 안에 다른 호출 문법(<parameter=…>, </parameter> 같은 표시)이 "
                    f"섞여 들어와 인자가 잘못 나뉘었습니다 (경로·내용 문제가 아님). 인자마다 값을 따로 넣어")
    return ""


def _parse_tool_call(tc: Any) -> ToolCallRequest:
    raw = tc.function.arguments
    call_id = tc.id or f"call_{uuid.uuid4().hex[:8]}"
    # Gemini 3 은 도구 호출마다 사고 서명(extra_content.google.thought_signature)을 주고, 다음 요청에 그대로 받아야 한다
    extra = {"extra_content": x} if (x := (getattr(tc, "model_extra", None) or {}).get("extra_content")) else {}
    if isinstance(raw, dict):
        return ToolCallRequest(call_id, tc.function.name, raw, error=leaked_call_syntax(raw), extra=extra)
    if not raw or not raw.strip():
        return ToolCallRequest(call_id, tc.function.name, {}, extra=extra)
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        # 소형 모델은 따옴표 누락, 끝 쉼표 같은 JSON 실수를 자주 한다. 고칠 수 있으면 고친다.
        args = json_repair.loads(raw)
    if not isinstance(args, dict):
        return ToolCallRequest(call_id, tc.function.name, {}, error=f"인자가 JSON 객체가 아닙니다: {raw[:200]}", extra=extra)
    return ToolCallRequest(call_id, tc.function.name, args, error=leaked_call_syntax(args), extra=extra)


def _plain(messages: list[dict[str, Any]], keep_extra: bool = False) -> list[dict[str, Any]]:
    """보낼 메시지에서 다른 공급사용 값('_raw' = Claude 블록)을 뺀다. 로컬 서버에는 extra_content 도 빼고 보낸다."""
    if not any("_raw" in m or (not keep_extra and m.get("tool_calls")) for m in messages):
        return messages
    out = []
    for m in messages:
        m = {k: v for k, v in m.items() if k != "_raw"}
        if not keep_extra and m.get("tool_calls"):
            m["tool_calls"] = [{k: v for k, v in c.items() if k != "extra_content"} for c in m["tool_calls"]]
        out.append(m)
    return out


def make_client(backend: Backend, sampling: Sampling) -> Any:
    """Backend.provider 에 맞는 모델 클라이언트 (local·openai·gemini → LLMClient, anthropic → AnthropicClient)."""
    if backend.provider == "anthropic":
        from agent.llm_anthropic import AnthropicClient
        return AnthropicClient(backend, sampling)
    return LLMClient(backend, sampling)


def assistant_message(resp: LLMResponse) -> dict[str, Any]:
    """모델 응답을 다음 턴에 다시 보낼 메시지로 바꾼다. 사고 과정(reasoning)은 다시 보내지 않는다."""
    msg: dict[str, Any] = {"role": "assistant", "content": resp.content}
    if resp.tool_calls:
        msg["tool_calls"] = [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}, **c.extra}
            for c in resp.tool_calls
        ]
    if resp.raw is not None:
        msg["_raw"] = resp.raw
    return msg
