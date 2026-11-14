"""CLI 의 '/' 명령 목록 — 입력창에서 / 를 치면 보여 준다.

Claude Code: 연결 직후 SDK get_server_info()['commands'] ({name, description, argumentHint, aliases?, builtin?}).
Gemini: ACP session/update 의 available_commands_update ({name, description, input{hint}}).
Codex: app-server 가 명령 목록을 주지 않는다 (화면의 웹 명령만).

엔진마다 마지막으로 받은 목록을 DB 설정(<엔진>_commands)에 남겨, CLI 를 아직 띄우지 않은 새 대화에서도 보여 준다.
한 번도 받은 적 없으면 아래 기본 목록.
"""
from __future__ import annotations

from typing import Any

# 웹에서 쓸 일이 없는 명령 (터미널 화면 전용 · 내부용)
HIDDEN = {"heapdump", "color", "focus", "workflow-launch-exec"}

# 자주 쓰는 기본 명령은 한국어 설명으로 보여 준다 (원문은 description_en)
KO = {
    "compact": "지금까지 대화를 요약해 컨텍스트를 비운다 (뒤에 요약 지침을 덧붙일 수 있음)",
    "context": "컨텍스트 사용량 보기",
    "usage": "이 세션의 비용 · 요금제 사용량 보기",
    "init": "이 폴더의 코드를 살펴 CLAUDE.md 를 만든다",
    "code-review": "현재 변경 사항(또는 PR · 브랜치 · 경로)을 검토해 버그를 찾는다",
    "security-review": "현재 브랜치의 변경 사항 보안 검토",
    "simplify": "바뀐 코드를 더 단순하고 효율적으로 정리한다",
    "debug": "디버그 기록을 켜고 문제 원인 찾기를 돕는다",
    "verify": "바꾼 코드가 실제로 의도대로 동작하는지 끝까지 돌려 확인한다",
    "run": "이 프로젝트의 앱을 띄워 변경 사항이 동작하는지 본다",
    "doctor": "Claude Code 설치 · 설정 점검",
    "mcp": "MCP 서버 관리 (다시 연결 · 켜기 · 끄기)",
    "batch": "큰 변경을 계획해 여러 작업 폴더에서 나눠 실행한다",
    "loop": "프롬프트나 명령을 일정 간격으로 되풀이한다",
    "goal": "목표를 정하고 그 조건이 될 때까지 계속 일한다",
    "recap": "이 세션을 한 줄로 요약",
    "insights": "Claude Code 세션 기록 분석 보고서",
    "output-style": "답변 스타일 보기 · 바꾸기",
    "advisor": "중요한 순간에 더 강한 모델에게 물어보게 한다",
    "autocompact": "자동 요약(autocompact) 기준 설정",
    "fast": "빠른 모드 켜기 · 끄기",
    "config": "설정 값 바꾸기 (key=value)",
    "reload-skills": "디스크에서 바뀐 스킬 다시 읽기",
    "reload-plugins": "바뀐 플러그인 적용",
    "skill-doctor": "안 쓰는 스킬 · 컨텍스트 비용 보기",
    "fewer-permission-prompts": "자주 승인한 읽기 전용 명령을 허용 목록에 넣어 승인 묻기를 줄인다",
    "deep-research": "웹을 넓게 찾아 근거를 확인하고 출처가 달린 보고서를 쓴다",
    "schedule": "예약 실행(루틴) 만들기 · 보기",
    "review": "현재 변경 사항 검토",
    "cost": "이 세션의 비용 보기",
}

# 한 번도 CLI 를 띄우지 않았을 때 (Claude Code 2.1 기준, 웹에서 쓸 만한 것만)
DEFAULTS: dict[str, list[dict[str, Any]]] = {
    "claude": [
        {"name": "compact", "argumentHint": "<optional custom summarization instructions>", "builtin": True},
        {"name": "context", "builtin": True},
        {"name": "usage", "aliases": ["cost", "stats"], "builtin": True},
        {"name": "init", "builtin": True},
        {"name": "code-review", "argumentHint": "[low|medium|high|xhigh|max] [<pr#>|<branch>|<path>]", "aliases": ["review"],
         "builtin": True},
        {"name": "security-review", "builtin": True},
        {"name": "simplify", "argumentHint": "[<target>]", "builtin": True},
        {"name": "debug", "argumentHint": "[issue description]", "builtin": True},
        {"name": "mcp", "argumentHint": "[reconnect|enable|disable [<server>|all]]", "builtin": True},
    ],
}


def _one(name: str, description: str, hint: str = "", aliases: list[str] | None = None, builtin: bool = False) -> dict[str, Any]:
    name = name.strip().lstrip("/")
    return {"name": name, "description": KO.get(name) or description.strip(), "description_en": description.strip(),
            "hint": hint.strip(), "aliases": [a.lstrip("/") for a in aliases or [] if a], "builtin": builtin}


def _keep(name: str, description: str) -> bool:
    return bool(name) and not name.startswith("_") and name not in HIDDEN \
        and not description.startswith(("(removed)", "Renamed to"))


def ordered(cmds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """자주 쓰는 명령(KO 순서)을 먼저, 나머지는 이름 순 — 화면은 거를 때 같은 점수면 이 순서를 지킨다."""
    order = {name: i for i, name in enumerate(KO)}
    return sorted(cmds, key=lambda c: (order.get(c["name"], len(order)), c["name"]))


def from_claude(raw: Any) -> list[dict[str, Any]]:
    out = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        name, desc = str(c.get("name") or "").strip().lstrip("/"), str(c.get("description") or "")
        if _keep(name, desc):
            aliases = c.get("aliases") if isinstance(c.get("aliases"), list) else []
            out.append(_one(name, desc, str(c.get("argumentHint") or ""), [str(a) for a in aliases], bool(c.get("builtin"))))
    return ordered(out)


def from_gemini(raw: Any) -> list[dict[str, Any]]:
    out = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        name, desc = str(c.get("name") or "").strip().lstrip("/"), str(c.get("description") or "")
        hint = (c.get("input") or {}).get("hint") if isinstance(c.get("input"), dict) else ""
        if _keep(name, desc):
            out.append(_one(name, desc, str(hint or "")))
    return ordered(out)


def defaults(engine: str) -> list[dict[str, Any]]:
    return from_claude(DEFAULTS.get(engine, [])) if engine == "claude" else []
