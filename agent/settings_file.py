"""관리 화면 '설정 › 실행 파라미터'에서 config/settings.yaml 의 값을 고친다.

settings.yaml 은 사람이 주석을 달아 관리하는 파일이라 통째로 다시 쓰지 않는다 (주석이 사라진다).
고칠 키의 줄만 찾아 값 부분만 바꾸고, 줄 끝 주석과 주석 위치는 그대로 둔다. 없는 키는 그 구역 끝에 넣는다.
바꾼 뒤에는 YAML 로 다시 읽어 값이 정확히 들어갔는지, Settings 로 검증되는지 확인한다.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from agent.config import Settings

# 화면에 보일 항목. type: float | int | bool | str | list | select · nullable: 비우면 키를 지워 기본값을 쓴다
PARAMS: dict[str, dict[str, Any]] = {
    "sampling": {
        "title": "생성 파라미터",
        "help": "모델이 답을 만들 때의 값. 모든 프로필에 적용되고, 프로필에서 온도·top_p 를 따로 정할 수 있습니다.",
        "fields": [
            {"key": "temperature", "label": "온도 (temperature)", "type": "float", "min": 0, "max": 2, "step": 0.05,
             "help": "높을수록 다양하게, 낮을수록 일정하게. Qwen 권장 0.6. 0 은 같은 말 반복에 빠지기 쉽습니다."},
            {"key": "top_p", "label": "top_p", "type": "float", "min": 0.05, "max": 1, "step": 0.05,
             "help": "확률이 높은 후보부터 합쳐 이 비율까지만 고릅니다. Qwen 권장 0.95 (생각 끔이면 0.8)."},
            {"key": "top_k", "label": "top_k", "type": "int", "min": 0, "max": 200, "nullable": True,
             "help": "후보를 상위 k개로 제한. 비우면 모델 서버 기본값(40). Qwen 권장 20."},
            {"key": "min_p", "label": "min_p", "type": "float", "min": 0, "max": 1, "step": 0.01, "nullable": True,
             "help": "가장 높은 후보 대비 이 비율보다 낮은 후보를 버립니다. 비우면 서버 기본값(0.05). Qwen 권장 0."},
            {"key": "presence_penalty", "label": "presence_penalty", "type": "float", "min": -2, "max": 2,
             "step": 0.1, "nullable": True,
             "help": "이미 나온 말을 피하게 합니다. 같은 문장을 되풀이하면 0.5~1.5. 비우면 0."},
            {"key": "max_tokens", "label": "응답 최대 토큰", "type": "int", "min": 64, "max": 65536, "nullable": True,
             "help": "한 번에 생성할 최대 길이. 비우면 제한 없음 (컨텍스트 안에서)."},
        ],
    },
    "limits": {
        "title": "한도",
        "help": "실행 한 번의 상한. 입력창의 '스텝' 선택이 있으면 그 값이 우선합니다.",
        "fields": [
            {"key": "max_steps", "label": "최대 스텝", "type": "int", "min": 0, "max": 1000,
             "help": "모델을 부르는 최대 횟수. 0 = 무제한. 프로필에서 따로 정할 수 있습니다."},
            {"key": "max_seconds", "label": "최대 시간(초)", "type": "int", "min": 0, "max": 86400,
             "help": "실행 한 번의 전체 제한 시간. 0 = 무제한."},
            {"key": "tool_output_chars", "label": "도구 출력 최대 글자", "type": "int", "min": 500, "max": 100000,
             "help": "이보다 긴 도구 출력은 앞·뒤만 모델에게 보여 줍니다 (전체는 기록에 저장). 크게 하면 컨텍스트가 빨리 찹니다."},
            {"key": "command_timeout", "label": "명령 제한 시간(초)", "type": "int", "min": 5, "max": 3600,
             "help": "run_command · run_python 한 번의 최대 실행 시간."},
        ],
    },
    "policy": {
        "title": "명령 실행 규칙",
        "help": "run_command 에 거는 규칙 (개발 규격 '의존성·설치': 프로젝트 로컬만, 폐쇄망 전제).",
        "fields": [
            {"key": "block_global_install", "label": "전역 설치 막기", "type": "bool",
             "help": "venv 밖 pip, npm -g, dotnet tool -g, winget 등 시스템 전역 설치를 막습니다."},
            {"key": "block_downloads", "label": "인터넷 다운로드 막기", "type": "bool",
             "help": "Invoke-WebRequest, curl, git clone 등을 막습니다."},
            {"key": "require_env_check", "label": "설치 전 환경 확인 요구", "type": "bool",
             "help": "프로젝트에 설치하기 전에 pip list · npm ls · dotnet --list-sdks 같은 확인을 먼저 하게 합니다."},
        ],
    },
    "web": {
        "title": "웹 검색 · 페이지 읽기",
        "help": "researcher 프로필의 web_search · fetch_url.",
        "fields": [
            {"key": "search_provider", "label": "검색 제공자", "type": "select", "options": ["ddgs", "searxng", "none"],
             "help": "ddgs = 인터넷(DuckDuckGo) · searxng = 사내 SearXNG · none = 검색 끔 (폐쇄망)."},
            {"key": "searxng_url", "label": "SearXNG 주소", "type": "str", "help": "예) http://searxng.intra:8080"},
            {"key": "regions", "label": "검색 지역", "type": "list",
             "help": "쉼표로 구분. kr-kr = 한국, us-en = 미국, wt-wt = 지역 없음."},
            {"key": "max_results", "label": "최대 결과 수", "type": "int", "min": 1, "max": 30, "help": "지역 합계."},
            {"key": "fetch_timeout", "label": "페이지 읽기 제한(초)", "type": "float", "min": 1, "max": 120, "step": 1,
             "help": ""},
        ],
    },
    "images": {
        "title": "이미지 보관",
        "help": "사내 API 에서 받아 채팅에 보여 준 이미지의 원본 사본 (data/runs/<실행>/images). 썸네일은 대화를 지울 때까지 남습니다.",
        "fields": [
            {"key": "keep_days", "label": "원본 보관 날수", "type": "int", "min": 1, "max": 3650,
             "help": "이 날수가 지난 원본·크게 보기 사본은 지웁니다 (관리 서버를 켤 때와 실행을 시작할 때). 지난 대화는 썸네일만 보입니다."},
        ],
    },
    "backend": {
        "title": "모델 호출",
        "help": "모델 서버 연결. 컨텍스트 길이는 모델 목록에서 따로 정한 모델이면 그 값이 우선합니다.",
        "fields": [
            {"key": "context_length", "label": "기본 컨텍스트 길이", "type": "int", "min": 2048, "max": 262144,
             "help": "모델 서버(-c)와 에이전트의 컨텍스트 예산. 바꾸면 모델 서버를 다시 켜야 적용됩니다."},
            {"key": "timeout", "label": "모델 호출 제한(초)", "type": "float", "min": 10, "max": 3600, "step": 10,
             "help": "모델 호출 한 번의 최대 대기 시간."},
        ],
    },
}


def _fmt(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):   # YAML 은 1e-05 를 숫자로 안 읽으므로 소수점 형식으로
        return repr(v) if "e" not in repr(v) else f"{v:.12f}".rstrip("0")
    out = yaml.safe_dump(v, default_flow_style=True, allow_unicode=True, width=10_000).strip()
    return out[:-3].strip() if out.endswith("...") else out


def _coerce(field: dict[str, Any], v: Any) -> Any:
    if v is None or v == "":
        if field.get("nullable"):
            return None
        if field["type"] == "str":
            return ""
        raise ValueError(f"'{field['label']}' 값을 입력하세요")
    t = field["type"]
    try:
        if t == "bool":
            if not isinstance(v, bool):
                raise ValueError
            return v
        if t == "int":
            if isinstance(v, bool) or float(v) != int(float(v)):
                raise ValueError
            v = int(float(v))
        elif t == "float":
            v = float(v)
            v = int(v) if v.is_integer() else v
        elif t == "list":
            items = v if isinstance(v, list) else str(v).split(",")
            return [str(x).strip() for x in items if str(x).strip()]
        elif t in ("str", "select"):
            v = str(v).strip()
            if t == "select" and v not in field["options"]:
                raise ValueError
            return v
    except (TypeError, ValueError):
        raise ValueError(f"'{field['label']}' 값이 올바르지 않습니다: {v}") from None
    if "min" in field and v < field["min"] or "max" in field and v > field["max"]:
        raise ValueError(f"'{field['label']}' 는 {field.get('min')} ~ {field.get('max')} 사이로 입력하세요")
    return v


def _section_bounds(lines: list[str], section: str) -> tuple[int, int]:
    """(구역 머리 줄, 구역 끝 다음 줄). 없으면 맨 끝에 새 구역을 만든다."""
    head = re.compile(rf"^{re.escape(section)}:\s*(#.*)?$")
    for i, line in enumerate(lines):
        if head.match(line):
            end = i + 1
            while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t#"):
                end += 1
            return i, end
        if re.match(rf"^{re.escape(section)}:\s*\S", line):
            raise ValueError(f"settings.yaml 의 '{section}' 이 한 줄 형식이라 화면에서 고칠 수 없습니다. 파일에서 직접 고치세요")
    lines += ["", f"{section}:"]
    return len(lines) - 1, len(lines)


def set_values(text: str, section: str, values: dict[str, Any]) -> str:
    """settings.yaml 내용(text)의 section 구역에서 values 의 키들만 바꾼 새 내용을 돌려준다 (None = 키 지우기)."""
    lines = text.splitlines()
    for key, value in values.items():
        start, end = _section_bounds(lines, section)
        body = [(i, lines[i]) for i in range(start + 1, end) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
        indent = re.match(r"^(\s+)", body[0][1]).group(1) if body else "  "
        rx = re.compile(rf"^({re.escape(indent)}){re.escape(key)}:(\s*)(.*?)(\s+#.*)?$")
        hit = next((i for i, line in body if rx.match(line)), None)
        if hit is None:
            if value is not None:
                pos = (body[-1][0] if body else start) + 1
                while pos < end and lines[pos].startswith((" ", "\t")) and lines[pos].lstrip().startswith("#"):
                    pos += 1   # 앞 키에 이어 붙은 설명 주석 줄은 건너뛴다
                lines.insert(pos, f"{indent}{key}: {_fmt(value)}")
            continue
        m = rx.match(lines[hit])
        stop = hit + 1   # 값이 다음 줄들에 이어지는 형식 (- 목록 등) 이면 그 줄들도 바꾼다
        if not m.group(3):
            while stop < end and (not lines[stop].strip() or len(lines[stop]) - len(lines[stop].lstrip()) > len(indent)):
                stop += 1
        if value is None:
            del lines[hit:stop]
            continue
        new = f"{indent}{key}: {_fmt(value)}"
        if m.group(4):   # 줄 끝 주석은 원래 자리(열)에 둔다
            col = len(m.group(1)) + len(key) + 1 + len(m.group(2)) + len(m.group(3)) + \
                (len(m.group(4)) - len(m.group(4).lstrip()))
            new += " " * max(1, col - len(new)) + m.group(4).lstrip()
        lines[hit:stop] = [new]
    out = "\n".join(lines) + "\n"
    raw = yaml.safe_load(out) or {}
    for key, value in values.items():
        got = (raw.get(section) or {}).get(key, None)
        if value is not None and got != value:
            raise ValueError(f"settings.yaml 에 {section}.{key} 를 쓰지 못했습니다 (형식이 예상과 다릅니다). 파일에서 직접 고치세요")
    Settings.model_validate(raw)
    return out


def update(text: str, section: str, values: dict[str, Any]) -> str:
    """화면에서 보낸 값을 검사·변환한 뒤 set_values 로 반영한다."""
    spec = PARAMS.get(section)
    if spec is None:
        raise ValueError(f"고칠 수 없는 구역입니다: {section}")
    fields = {f["key"]: f for f in spec["fields"]}
    unknown = set(values) - set(fields)
    if unknown:
        raise ValueError(f"고칠 수 없는 항목입니다: {', '.join(sorted(unknown))}")
    return set_values(text, section, {k: _coerce(fields[k], v) for k, v in values.items()})


def file_values(text: str) -> dict[str, dict[str, Any]]:
    """settings.yaml 에 실제로 적힌 값 (없으면 키가 없다)."""
    raw = yaml.safe_load(text) or {}
    return {sec: dict(raw.get(sec) or {}) for sec in PARAMS}
