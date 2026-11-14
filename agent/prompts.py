"""프롬프트 저장소와 조립 규칙.

모델에게 가는 글은 전부 여기서 나온다.
- 블록(blocks): 시스템 프롬프트 조각. 프로필이 쓸 블록과 순서를 고른다 (Profile.blocks).
- 메시지(messages): 루프가 끼어들 때 모델에게 하는 말 (되돌려 보내기, 한도, 반복·실패 경고 …).
- 도구 설명(tools): 모델이 보는 도구 설명. 비어 있으면 코드의 docstring.
- 변수(variables): {{이름}} 자리에 들어갈 값. 내장 변수 > 프로필 변수 > 전역 변수 순서로 찾는다.

기본값은 agent/prompt_defaults/prompts.yaml (배포 원본), 사용자가 바꾼 것만 config/prompts.yaml 에 둔다.
그래서 '기본값으로'는 config 쪽 항목을 지우는 것이고, 프로그램을 업데이트하면 안 바꾼 항목은 새 기본값을 따른다.

문법
    {{이름}}            변수 자리
    줄 맨 앞 {{?이름}}   그 도구를 가진 프로필이거나 그 변수가 비어 있지 않을 때만 그 줄을 넣는다
    {{?a|b}} 하나라도 · {{?!이름}} 반대
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from agent import config

DEFAULTS_DIR = Path(__file__).resolve().parent / "prompt_defaults"
DEFAULTS_FILE = DEFAULTS_DIR / "prompts.yaml"
DEFAULT_AGENTS_DIR = DEFAULTS_DIR / "agents"   # 기본 프로필 원본 ('기본값으로' 되돌릴 때)
USER_FILE = config.CONFIG_DIR / "prompts.yaml"

# 프로그램이 내용을 채우는 블록. 프로필의 blocks 에 넣으면 그 자리에 들어간다
DYNAMIC_BLOCKS = {
    "intranet_status": ("사내 연결 현황 (자동)",
                        "등록된 사내 API·사이트·허용 주소. 사내 도구(api_get, search_intranet …)를 가진 프로필에만 채워진다"),
    "lang_standard": ("언어별 규격 (자동)",
                      "개발 규격을 쓰는 프로필이 개발 언어를 알아채면 그 언어 규격을 붙인다. 실행 중에 붙으므로 맨 뒤에 둔다"),
}
BUILTIN_VARS = {
    "cwd": "작업 폴더 (상대 경로의 기준. 대화 전용 폴더가 있으면 그 폴더)",
    "session_folder": "대화 전용 폴더. 없으면 빈 값이라 {{?session_folder}} 조건 줄에 쓴다",
    "log_name": "프로그램이 정리하는 작업기록 파일 이름",
    "dirs": "파일 도구로 접근할 수 있는 폴더 목록 (여러 줄)",
    "shell": "명령 실행 셸",
    "today": "오늘 날짜 (YYYY-MM-DD)",
    "weekday": "오늘 요일",
    "profile": "프로필 이름",
    "model": "모델 이름",
    "context_length": "모델 컨텍스트 길이(토큰)",
    "tools": "이 프로필이 쓰는 도구 이름 (쉼표로 구분)",
}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")   # 블록·변수 이름
_VAR = re.compile(r"\{\{\s*([A-Za-z_][\w.-]*)\s*\}\}")
_COND = re.compile(r"^(\s*)\{\{\?(!?)([\w.|-]+)\}\}")
_ANY_VAR = re.compile(r"\{\{(?!\?)([^{}\n]+?)\}\}")
_HEADER = ("# 관리 화면 '설정'에서 바꾼 프롬프트 (기본값과 다른 것만). 직접 고쳐도 된다.\n"
           "# 항목을 지우면 기본값(agent/prompt_defaults/prompts.yaml)으로 돌아간다.\n")


@dataclass
class Block:
    id: str
    title: str
    description: str
    text: str
    default: str | None = None      # 기본 제공 블록의 원래 내용 (사용자가 만든 블록이면 None)
    dynamic: bool = False

    @property
    def modified(self) -> bool:
        return self.default is not None and self.text != self.default


@dataclass
class Message:
    key: str
    title: str
    when: str
    vars: dict[str, str]
    text: str
    default: str

    @property
    def modified(self) -> bool:
        return self.text != self.default


@dataclass
class Variable:
    name: str
    value: str
    description: str = ""
    default: str | None = None

    @property
    def modified(self) -> bool:
        return self.default is not None and self.value != self.default


@dataclass
class Prompts:
    blocks: dict[str, Block] = field(default_factory=dict)
    messages: dict[str, Message] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)        # 도구 이름 → 바꾼 설명
    variables: dict[str, Variable] = field(default_factory=dict)

    def values(self) -> dict[str, str]:
        return {v.name: v.value for v in self.variables.values()}

    def message(self, key: str, values: Mapping[str, str] | None = None, flags: Iterable[str] = (), **extra: Any) -> str:
        """루프 메시지를 변수로 채워 돌려준다. extra 는 그 메시지 전용 변수 (kind, count …)."""
        merged = {**self.values(), **(values or {}), **{k: str(v) for k, v in extra.items()}}
        return render(self.messages[key].text, merged, flags).strip()


# ───────────────────────── 조립

def render(text: str, values: Mapping[str, str], flags: Iterable[str] = ()) -> str:
    """조건 줄을 거르고 {{변수}}를 채운다. 모르는 변수는 그대로 둔다 (미리보기 경고로 알려 준다)."""
    on = set(flags) | {k for k, v in values.items() if str(v).strip()}
    lines = []
    for line in text.splitlines():
        m = _COND.match(line)
        if m:
            hit = any(name in on for name in m.group(3).split("|"))
            if hit == bool(m.group(2)):
                continue
            line = m.group(1) + line[m.end():]
        lines.append(line)
    out = "\n".join(lines)
    return _VAR.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), out)


def used_vars(text: str) -> set[str]:
    return set(_VAR.findall(text))


def unknown_vars(text: str, values: Mapping[str, str]) -> set[str]:
    """채워지지 않을 {{…}} (오타, 한글 이름처럼 변수 규칙에 안 맞는 것 포함). 조건 {{?…}} 은 제외."""
    return {m.strip() for m in _ANY_VAR.findall(text)} - set(values)


# ───────────────────────── 읽기

def _read_yaml(path: Path) -> dict[str, Any]:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


@lru_cache(maxsize=1)
def _defaults() -> dict[str, Any]:
    return _read_yaml(DEFAULTS_FILE)


def _norm(text: Any) -> str:
    return str(text or "").strip("\n") + "\n" if "\n" in str(text or "").strip() else str(text or "").strip()


def load_prompts(user_file: Path | None = None) -> Prompts:
    """기본값 위에 사용자가 바꾼 것을 덮어 실제로 쓸 프롬프트를 만든다. 실행할 때마다 새로 읽는다."""
    d, u = _defaults(), _read_yaml(user_file or USER_FILE)
    p = Prompts()
    for bid, b in (d.get("blocks") or {}).items():
        p.blocks[bid] = Block(bid, b.get("title", bid), b.get("description", ""), _norm(b["text"]), _norm(b["text"]))
    for bid, b in (u.get("blocks") or {}).items():
        if not isinstance(b, dict) or bid in DYNAMIC_BLOCKS:
            continue
        base = p.blocks.get(bid)
        p.blocks[bid] = Block(bid, b.get("title") or (base.title if base else bid),
                              b.get("description", base.description if base else ""),
                              _norm(b.get("text", base.text if base else "")), base.default if base else None)
    for bid, (title, desc) in DYNAMIC_BLOCKS.items():
        p.blocks[bid] = Block(bid, title, desc, "", None, dynamic=True)
    for key, m in (d.get("messages") or {}).items():
        p.messages[key] = Message(key, m.get("title", key), m.get("when", ""), dict(m.get("vars") or {}),
                                  _norm(m["text"]), _norm(m["text"]))
    for key, text in (u.get("messages") or {}).items():
        if key in p.messages and isinstance(text, str):
            p.messages[key].text = _norm(text)
    p.tools = {k: _norm(v) for k, v in (u.get("tools") or {}).items() if isinstance(v, str) and v.strip()}
    for name, v in (d.get("variables") or {}).items():
        p.variables[name] = Variable(name, str(v.get("value", "")), v.get("description", ""), str(v.get("value", "")))
    for name, v in (u.get("variables") or {}).items():
        v = v if isinstance(v, dict) else {"value": v}
        base = p.variables.get(name)
        p.variables[name] = Variable(name, str(v.get("value", "")), v.get("description") or (base.description if base else ""),
                                     base.default if base else None)
    return p


def default_message(key: str) -> str:
    return _norm(_defaults()["messages"][key]["text"]).strip()


# ───────────────────────── 쓰기 (관리 화면)

class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False):   # 목록도 들여 써서 손으로 쓴 파일과 같은 모양으로
        return super().increase_indent(flow, False)


def _repr_str(dumper: yaml.SafeDumper, s: str) -> yaml.Node:
    if "\n" in s:   # 여러 줄은 | 블록으로 (사람이 읽기 좋게). 줄 끝 공백이 있으면 PyYAML 이 블록을 못 쓰므로 지운다
        s = "\n".join(line.rstrip() for line in s.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", s)


_Dumper.add_representer(str, _repr_str)


def dump_yaml(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, allow_unicode=True, sort_keys=False, width=10_000)


def read_user(user_file: Path | None = None) -> dict[str, Any]:
    return _read_yaml(user_file or USER_FILE)


def user_text(data: dict[str, Any]) -> str:
    data = {k: v for k, v in data.items() if v}
    return _HEADER + (dump_yaml(data) if data else "{}\n")


def apply_change(data: dict[str, Any], section: str, key: str, value: Any) -> dict[str, Any]:
    """config/prompts.yaml 내용(data)에 바꾼 것 하나를 반영한다. value=None 이면 그 항목을 지운다 (기본값으로).
    기본값과 같아진 항목은 저장하지 않는다 (나중에 기본값이 좋아지면 그대로 따라가게)."""
    if section not in ("blocks", "messages", "tools", "variables"):
        raise ValueError(f"알 수 없는 구역: {section}")
    d = _defaults()
    items = dict(data.get(section) or {})
    if value is None:
        items.pop(key, None)
    elif section == "messages":
        if key not in (d.get("messages") or {}):
            raise ValueError(f"알 수 없는 메시지: {key}")
        text = _norm(value)
        if not text.strip():
            raise ValueError("메시지를 비울 수 없습니다. 기본값으로 되돌리려면 '기본값으로'를 누르세요")
        if text == _norm(d["messages"][key]["text"]):
            items.pop(key, None)
        else:
            items[key] = text
    elif section == "tools":
        from agent.tools import REGISTRY
        if key not in REGISTRY:
            raise ValueError(f"알 수 없는 도구: {key}")
        text = _norm(value)
        if not text.strip() or text.strip() == REGISTRY[key].description.strip():
            items.pop(key, None)
        else:
            items[key] = text
    elif section == "blocks":
        if not NAME_RE.match(key) or key in DYNAMIC_BLOCKS:
            raise ValueError("블록 이름은 영어 소문자로 시작하고 영어 소문자·숫자·_ 만 쓸 수 있습니다 (40자 이하)")
        base = (d.get("blocks") or {}).get(key)
        v = {"title": str(value.get("title") or "").strip(), "description": str(value.get("description") or "").strip(),
             "text": _norm(value.get("text"))}
        if not v["text"].strip():
            raise ValueError("블록 내용이 비어 있습니다")
        if base and v["text"] == _norm(base["text"]) and v["title"] in ("", base.get("title")) \
                and v["description"] in ("", base.get("description", "")):
            items.pop(key, None)
        else:
            items[key] = {k: x for k, x in v.items() if x}
    elif section == "variables":
        if not NAME_RE.match(key) or key in BUILTIN_VARS:
            raise ValueError("변수 이름은 영어 소문자로 시작하고 영어 소문자·숫자·_ 만 쓸 수 있습니다"
                             + (f" ('{key}'는 내장 변수라 쓸 수 없습니다)" if key in BUILTIN_VARS else ""))
        base = (d.get("variables") or {}).get(key)
        v = {"value": str(value.get("value", "")), "description": str(value.get("description") or "").strip()}
        if base and v["value"] == str(base.get("value", "")) and v["description"] in ("", base.get("description", "")):
            items.pop(key, None)
        else:
            items[key] = {k: x for k, x in v.items() if x or k == "value"}
    out = dict(data)
    out[section] = items
    return out
