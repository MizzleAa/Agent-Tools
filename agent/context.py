"""컨텍스트 관리: 도구 출력 자르기, 오래된 기록 줄이기.

모델의 컨텍스트(16k~32k 토큰)를 넘으면 llama-server 가 요청을 거절하고 실행이 죽는다.
대화에 쌓이는 것은 두 가지다.
  1) 도구 결과 (빌드·실행 오류 출력 등)            → role == "tool"
  2) 모델이 보낸 도구 인자 (파일 쓰기의 코드 원문 등) → assistant 의 tool_calls[].arguments
둘 다 오래된 것부터 줄이고, 그래도 넘치면 가장 오래된 턴을 통째로 지운다 (항상 줄일 대상이 남게).
시스템 프롬프트와 사용자 요청은 지우지 않는다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SHRUNK_NOTE = "\n…(오래된 도구 결과라 줄였습니다. 필요하면 다시 실행하세요)"
# 모델이 이 표시를 '이전에 보낸 값'으로 알고 그대로 베껴 쓰는 일이 있다 (QA R08: 파일 내용이 이 문구로 저장됨).
# 그래서 표시임을 앞에 밝히고, 도구 인자에 이 문구가 들어오면 실행하지 않는다 (leaked_note).
# 파일 내용이 이 표시로 바뀐 것으로 오해하지 않게(QA E9: "README 가 압축 표시 내용으로 저장됨"이라 보고) 실제 파일은 그대로임을 밝힌다
ARG_NOTE = "[기록 압축 표시] 이 호출로 보낸 {n}자는 그대로 실행·저장됐고, 기록을 줄이려고 생략함 (실제 파일은 그대로 — 내용은 read_file)"
DROPPED_NOTE = ("(컨텍스트 한도 때문에 이 앞의 작업 기록 일부를 지웠습니다. "
                "파일의 현재 상태는 list_dir·read_file 로 다시 확인하세요.)")
# QA S-7: 지운 기록에서 받은 값(리뷰 현황·이름 등)을 모델이 기억으로 지어냈다 → 지운 도구 결과의 한 줄 요약을 남긴다
KEPT_HEAD = "지운 기록에서 실제로 받은 결과 (요약 — 보고에는 이 값이나 다시 조회한 값만 쓴다):"
KEPT_MAX = 4000      # 남겨 둘 요약 최대 글자 (넘으면 긴 줄을 줄이고, 그래도 넘으면 오래된 줄부터 뺀다)
_NOTE_MARKS = re.compile(r"기록 압축 표시|기록을 줄이려고 생략|오래된 도구 결과라 줄였습니다|컨텍스트 한도 때문에 이 앞의 작업 기록")


def leaked_note(arguments: dict[str, Any]) -> str | None:
    """도구 인자에 기록 압축 표시 문구가 들어 있으면 그 인자 이름. 이 문구는 내용이 아니라 표시다."""
    for key, value in arguments.items():
        if isinstance(value, str) and _NOTE_MARKS.search(value):
            return key
    return None


BIG_ARG = 300        # 이보다 긴 문자열 인자는 오래되면 생략한다
BIG_TOOL = 400       # 이보다 긴 도구 결과는 오래되면 300자로 줄인다
BIG_TEXT = 1200      # 이보다 긴 assistant 본문은 오래되면 줄인다


def truncate_output(text: str, limit: int, save_to: Path | None = None, readable: bool = True) -> str:
    """limit 자를 넘으면 앞 60% + 뒤 30% 만 남긴다. 에러 메시지는 보통 끝에 있어서 뒤도 남긴다.
    readable: 모델이 save_to 를 도구로 읽을 수 있는지. 못 읽으면 그 경로를 알려 주지 않는다 (QA API-9: 읽을 수 없는
    파일을 안내받아 file://·지어낸 주소·실행할 수 없는 스크립트로 헤맸다)."""
    if len(text) <= limit:
        return text
    head, tail = text[: int(limit * 0.6)], text[-int(limit * 0.3):]
    middle = text[len(head): len(text) - len(tail)]
    note = f"\n\n... [중간 {middle.count(chr(10))}줄 / {len(middle)}자 생략"
    if save_to is not None:
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_text(text, encoding="utf-8")
        note += f". 전체 출력은 {save_to} 에 저장됨" if readable else ". 생략한 부분은 다시 볼 수 없음"
    note += ". 필요하면 범위를 좁혀 다시 요청하세요] ...\n\n"
    return head + note + tail


def chars_of(messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None = None) -> int:
    """요청 크기(글자). 도구 인자와 도구 스키마까지 센다."""
    total = 0
    for m in messages:
        total += len(m.get("content") or "") + 8
        for c in m.get("tool_calls") or []:
            total += len(c["function"]["arguments"]) + len(c["function"]["name"]) + 24
    if schemas:
        total += len(json.dumps(schemas, ensure_ascii=False))
    return total


def _shrink_call(call: dict[str, Any]) -> bool:
    text = call["function"]["arguments"]
    if len(text) <= BIG_ARG * 2:
        return False
    try:
        args = json.loads(text)
    except ValueError:
        args = None
    if not isinstance(args, dict):
        call["function"]["arguments"] = json.dumps({"_": ARG_NOTE.format(n=len(text))}, ensure_ascii=False)
        return True
    changed = False
    for key, value in args.items():
        if isinstance(value, str) and len(value) > BIG_ARG and not value.startswith("(앞서 보낸"):
            args[key] = ARG_NOTE.format(n=len(value))
            changed = True
    if changed:
        call["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
    return changed


def shrink_old(messages: list[dict[str, Any]], keep_last: int, digests: dict[str, str] | None = None) -> bool:
    """마지막 keep_last 개를 뺀 나머지에서 큰 도구 결과·큰 도구 인자·긴 본문을 줄인다. 줄인 게 있으면 True.
    digests: 도구 호출 id → 결과 한 줄 요약. 있으면 앞 300자 대신 요약을 남긴다 (앞부분은 머리말·id 뿐인 일이 많다)."""
    changed = False
    for m in messages[1:max(1, len(messages) - keep_last)]:
        content = m.get("content") or ""
        if m["role"] == "tool" and len(content) > BIG_TOOL and not content.endswith(SHRUNK_NOTE):
            kept = (digests or {}).get(m.get("tool_call_id") or "")
            m["content"] = (f"[요약] {kept}" if kept else content[:300]) + SHRUNK_NOTE
            changed = True
        elif m["role"] == "assistant":
            touched = False
            for call in m.get("tool_calls") or []:
                touched |= _shrink_call(call)
            if len(content) > BIG_TEXT:
                m["content"] = content[:BIG_TEXT // 2] + "\n…(오래된 설명이라 줄였습니다)"
                touched = True
            if touched and m.get("_raw"):
                _sync_raw(m)
            changed |= touched
    return changed


def _sync_raw(m: dict[str, Any]) -> None:
    """Claude 답(_raw: 공급사 블록 그대로)을 줄인 글·도구 인자에 맞춘다. 추론(thinking) 블록은 남긴다 —
    앞 대화가 바뀌어 맞지 않게 된 블록은 공급사가 버린다 (agent/llm_anthropic.py 의 drop_block)."""
    blocks = [b for b in m["_raw"] if b.get("type") in ("thinking", "redacted_thinking")]
    if text := str(m.get("content") or "").strip():
        blocks.append({"type": "text", "text": text})
    for call in m.get("tool_calls") or []:
        try:
            args = json.loads(call["function"]["arguments"])
        except ValueError:
            args = {}
        blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                       "input": args if isinstance(args, dict) else {}})
    m["_raw"] = blocks


def drop_oldest_turn(messages: list[dict[str, Any]], keep_last: int = 4, digests: dict[str, str] | None = None) -> bool:
    """가장 오래된 assistant 턴(그 도구 결과까지)을 지운다. 시스템·사용자 메시지·최근 keep_last 개는 지키지 않는다.
    digests 가 있으면 지운 도구 결과의 한 줄 요약을 '지웠다' 표시 아래에 모아 둔다 (QA S-7)."""
    limit = len(messages) - keep_last
    for i in range(1, limit):
        if messages[i]["role"] != "assistant":
            continue
        j = i + 1
        while j < len(messages) and messages[j]["role"] == "tool":
            j += 1
        if j > limit:
            return False
        kept = [digests[m["tool_call_id"]] for m in messages[i + 1:j]
                if digests and m.get("tool_call_id") in digests]
        del messages[i:j]
        at = next((k for k, m in enumerate(messages) if str(m.get("content") or "").startswith(DROPPED_NOTE)), None)
        if at is None:
            messages.insert(i, {"role": "user", "content": DROPPED_NOTE})
            at = i
        if kept:
            messages[at]["content"] = _keep(str(messages[at]["content"]), kept)
        return True
    return False


def _keep(note: str, lines: list[str]) -> str:
    """요약 줄을 모은다. 같은 호출은 마지막 것만, 넘치면 긴 줄부터 줄이고 그래도 넘치면 오래된 줄부터 뺀다."""
    old = note.split(KEPT_HEAD, 1)[1].strip().splitlines() if KEPT_HEAD in note else []
    rows: dict[str, str] = {}
    for r in [r for r in old if r.startswith("- ")] + [f"- {line}" for line in lines]:
        label = r.split(" → ", 1)[0]
        rows.pop(label, None)
        rows[label] = r
    kept = list(rows.values())
    size = lambda: sum(len(r) + 1 for r in kept)   # noqa: E731
    if size() > KEPT_MAX:
        kept = [r if len(r) <= 240 else r[:239] + "…" for r in kept]
    while len(kept) > 1 and size() > KEPT_MAX:
        kept.pop(0)
    return f"{DROPPED_NOTE}\n{KEPT_HEAD}\n" + "\n".join(kept)


def fit_to_budget(messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None, budget: int,
                  digests: dict[str, str] | None = None) -> bool:
    """요청 크기가 budget(글자) 안으로 들어올 때까지 단계적으로 줄인다. 줄인 게 있으면 True.
    1) 오래된 것부터 줄이고 → 2) 더 최근 것까지 줄이고 → 3) 오래된 턴을 지운다 (마지막까지 줄일 대상이 남는다)."""
    changed = False
    for keep in (8, 4, 2):
        if chars_of(messages, schemas) <= budget:
            return changed
        changed |= shrink_old(messages, keep, digests)
    while chars_of(messages, schemas) > budget and drop_oldest_turn(messages, keep_last=3, digests=digests):
        changed = True
    return changed


def compact_messages(messages: list[dict[str, Any]], budget: int, keep_last: int = 6) -> bool:
    """budget 을 넘으면(budget=0 이면 무조건) 마지막 keep_last 개를 뺀 오래된 기록을 줄인다. 예전 호출부 호환용."""
    if budget and chars_of(messages) <= budget:
        return False
    return shrink_old(messages, keep_last)
