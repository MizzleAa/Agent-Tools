"""프롬프트 미리보기: 용도(프로필) 하나로 실제 시스템 프롬프트·도구 설명을 조립하고, 크기를 재고, 구성을 점검한다.

관리 화면 '설정 › 용도 › 미리보기'가 쓴다. 저장하지 않은 프로필(초안)도 그대로 넣을 수 있다.
토큰 수는 모델 서버가 켜져 있으면 /tokenize 로 정확히, 꺼져 있으면 글자 수로 어림한다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import httpx

from agent import prompts, standards
from agent.config import Settings
from agent.loop import CTX_RESERVE, INTRANET_TOOLS, Agent
from agent.profiles import MAX_TOOLS, Profile
from agent.tools import REGISTRY

VOLATILE_BLOCKS = {"environment"}   # 대화마다 바뀌는 값(작업 폴더·날짜)이 든 블록
BUDGET_WARN = 0.35                  # 시스템 프롬프트 + 도구 설명이 컨텍스트 예산의 이 비율을 넘으면 경고


class _ModelName:
    def __init__(self, model: str):
        self.model = model


def estimate_tokens(text: str) -> int:
    """어림값: 한글 한 글자 ≈ 0.9 토큰, 그 밖(영어·기호·JSON) 한 글자 ≈ 0.3 토큰 (Qwen 토크나이저 기준 대략)."""
    hangul = sum(1 for ch in text if "가" <= ch <= "힣")
    return round(hangul * 0.9 + (len(text) - hangul) * 0.3)


def tokenizer(settings: Settings) -> tuple[Callable[[str], int], Callable[[], None]] | None:
    """모델 서버가 켜져 있으면 (토큰 세기, 닫기). 꺼져 있으면 None."""
    base = re.sub(r"/v1/?$", "", settings.backend.base_url)
    client = httpx.Client(timeout=httpx.Timeout(5, connect=1))   # 꺼져 있으면 빨리 포기하고 어림으로
    try:
        client.post(f"{base}/tokenize", json={"content": "확인"}).raise_for_status()
    except (httpx.HTTPError, OSError):
        client.close()
        return None
    return (lambda text: len(client.post(f"{base}/tokenize", json={"content": text}).json().get("tokens", []))), client.close


def lint(agent: Agent, profile: Profile, p: prompts.Prompts) -> list[dict[str, str]]:
    """프롬프트 구성 점검. level: warn(고치는 게 좋음) | info(참고)."""
    out: list[dict[str, str]] = []
    warn = lambda text: out.append({"level": "warn", "text": text})   # noqa: E731
    info = lambda text: out.append({"level": "info", "text": text})   # noqa: E731
    blocks = profile.effective_blocks()
    values = agent.variables()
    missing = [b for b in blocks if b not in p.blocks]
    if missing:
        warn(f"없는 블록이 구성에 있습니다: {', '.join(missing)} (실행할 때는 건너뜁니다)")
    texts = [("역할 프롬프트", profile.system_prompt)] + [(p.blocks[b].title, p.blocks[b].text) for b in blocks if b in p.blocks]
    for title, text in texts:
        unknown = prompts.unknown_vars(text, values)
        if unknown:
            warn(f"'{title}'에 정의되지 않은 변수가 있습니다: {', '.join('{{' + v + '}}' for v in sorted(unknown))}"
                 " — '변수' 탭에서 만들거나 고치세요")
    tools = set(agent.tools)   # 용도의 도구 + 저절로 붙는 것 (하네스 자료가 있으면 search_knowledge · read_knowledge)
    if tools & INTRANET_TOOLS and "intranet_status" not in blocks:
        warn("사내 도구가 있는데 '사내 연결 현황' 블록이 빠져 있습니다. 모델이 등록된 API·사이트를 모릅니다")
    if profile.standards:
        if "dev_standard" not in blocks:
            warn("'개발 규격 적용'이 켜져 있는데 '개발 규격 요약' 블록이 빠져 있습니다 (끝날 때 점검만 하고 규격은 안 알려 주게 됩니다)")
        if "lang_standard" not in blocks:
            warn("'개발 규격 적용'이 켜져 있는데 '언어별 규격' 블록이 빠져 있어 언어 규격이 붙지 않습니다")
        if "read_standard" not in tools:
            info("read_standard 도구가 없으면 모델이 공통 규격 전문을 직접 읽을 수 없습니다")
    elif "dev_standard" in blocks:
        info("'개발 규격 요약' 블록이 있지만 '개발 규격 적용'이 꺼져 있어 끝날 때 점검·언어별 규격은 하지 않습니다")
    if tools & {"run_command", "run_python"} and "environment" not in blocks:
        warn("명령 실행 도구가 있는데 '실행 환경' 블록이 없습니다. 모델이 셸·작업 폴더를 모릅니다 (bash 명령을 쓰기 쉬움)")
    written = "\n".join(text for bid, _, text in agent.prompt_sections() if bid not in prompts.DYNAMIC_BLOCKS)
    mentioned = sorted(n for n in REGISTRY if n not in tools and re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", written))
    if mentioned:
        warn(f"프롬프트가 이 프로필에 없는 도구를 언급합니다: {', '.join(mentioned)} — 모델이 없는 도구를 부르려 할 수 있습니다 "
             "(도구를 켜거나, 그 문장을 지우거나, 줄 앞에 {{?도구}} 조건을 다세요)")
    volatile = [i for i, b in enumerate(blocks) if b in VOLATILE_BLOCKS]
    stable_after = [b for b in blocks[(volatile[0] + 1 if volatile else len(blocks)):]
                    if b not in VOLATILE_BLOCKS and b not in prompts.DYNAMIC_BLOCKS]
    if stable_after:
        info(f"'실행 환경'(대화마다 바뀜) 뒤에 {', '.join(stable_after)} 가 있습니다. 바뀌지 않는 블록을 앞에 두면 "
             "새 대화의 첫 응답이 빨라집니다 (모델 서버가 앞부분 계산을 재사용)")
    if "lang_standard" in blocks and blocks[-1] != "lang_standard":
        info("'언어별 규격'은 실행 도중에 붙으므로 맨 뒤에 두는 게 좋습니다 (그 앞 계산을 재사용)")
    if len(profile.tools) > 8:
        info(f"도구가 {len(profile.tools)}개입니다. 소형 모델은 도구가 많을수록 엉뚱한 도구를 고르기 쉽습니다 (최대 {MAX_TOOLS})")
    if profile.temperature is not None and profile.temperature < 0.1:
        warn("온도가 0에 가까우면 같은 말을 되풀이하기 쉽습니다 (Qwen 권장 0.6)")
    if profile.auto_approve_writes:
        info("쓰기 자동 승인이 켜져 있습니다. 허용된 쓰기 폴더 안의 파일 저장은 묻지 않습니다")
    return out


def preview(settings: Settings, profile: Profile) -> dict[str, Any]:
    """조립 결과 + 조각별 크기 + 예산 비율 + 점검 경고. 없는 도구가 있으면 ValueError."""
    agent = Agent(settings, profile, llm=_ModelName(settings.backend.model))   # type: ignore[arg-type]
    counter = tokenizer(settings)
    measure = counter[0] if counter else estimate_tokens
    try:
        sections = [{"id": bid, "title": title, "text": text, "chars": len(text), "tokens": measure(text)}
                    for bid, title, text in agent.prompt_sections()]
        schemas = agent.tool_schemas()
        tools_json = "\n".join(json.dumps(x, ensure_ascii=False) for x in schemas)
        tools_tokens = measure(tools_json)
        lang_extra = {}
        if profile.standards and "lang_standard" in profile.effective_blocks():
            lang_extra = {standards.LANG_LABEL[lang]: measure(agent._lang_section(lang)) for lang in standards.LANG_LABEL}
    finally:
        if counter:
            counter[1]()
    system_tokens = sum(x["tokens"] for x in sections) + 4 * len(sections)   # 채팅 템플릿 머리 몫
    total = system_tokens + tools_tokens
    ctx = settings.backend.context_length
    budget = ctx - min(CTX_RESERVE, ctx // 4)
    warnings = lint(agent, profile, prompts.load_prompts())
    if total > budget * BUDGET_WARN:
        warnings.insert(0, {"level": "warn", "text": f"시스템 프롬프트와 도구 설명이 컨텍스트 예산의 {round(total / budget * 100)}% 입니다. "
                                                     "대화·도구 결과에 쓸 자리가 줄어 일찍 잘립니다"})
    return {
        "prompt": agent.system_prompt(), "sections": sections,
        "tools": {"count": len(schemas), "tokens": tools_tokens, "chars": len(tools_json), "json": tools_json},
        "system_tokens": system_tokens, "total_tokens": total, "exact": counter is not None, "context_length": ctx,
        "budget": budget, "lang_extra": lang_extra, "warnings": warnings, "model": settings.backend.model,
    }
