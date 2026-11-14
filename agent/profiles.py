"""에이전트 프로필: 역할(시스템 프롬프트) + 쓸 블록 + 쓸 수 있는 도구 + 파라미터를 묶은 단위.

config/agents/<name>.yaml 하나가 프로필 하나다. 관리 화면 '설정'에서 고치면 이 파일이 바뀐다.
하네스의 역할 파일(harness/agents/<name>.md — agent/harness.py)도 프로필이다. 이름이 같으면 하네스 쪽이 우선한다.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.config import CONFIG_DIR

AGENTS_DIR = CONFIG_DIR / "agents"
MAX_TOOLS = 10  # 소형 모델은 도구가 많을수록 엉뚱한 도구를 고른다 (Qwen3.5 급이면 10개까지 무난)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
FIELD_ORDER = ("name", "description", "system_prompt", "blocks", "tools", "max_steps", "think", "temperature",
               "top_p", "auto_approve_writes", "standards", "variables")


class Profile(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)   # API 응답 문서에서는 기본값 필드도 '항상 옴'
    name: str
    description: str = ""
    system_prompt: str                 # 역할 프롬프트. 시스템 프롬프트 맨 앞에 들어간다
    blocks: list[str] | None = None    # 역할 뒤에 붙일 블록(agent/prompts.py)과 순서. None 이면 default_blocks()
    tools: list[str]
    max_steps: int | None = None       # None 이면 settings.limits.max_steps
    think: bool | None = None          # None 이면 모델 기본값
    temperature: float | None = Field(default=None, ge=0, le=2)   # None 이면 settings.sampling
    top_p: float | None = Field(default=None, gt=0, le=1)
    auto_approve_writes: bool = False  # True 면 쓰기 권한 폴더에 대한 쓰기를 묻지 않음
    standards: bool = False            # True 면 개발 규격을 따르게 한다: 언어별 규격 붙이기 + 끝날 때 점검
    variables: dict[str, str] = Field(default_factory=dict)   # 이 프로필에서만 쓰는 {{변수}} (전역 변수보다 우선)

    @field_validator("tools")
    @classmethod
    def _limit_tools(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_TOOLS:
            raise ValueError(f"도구는 프로필당 {MAX_TOOLS}개 이하로 두세요 (현재 {len(v)}개)")
        if len(set(v)) != len(v):
            raise ValueError("같은 도구가 두 번 들어 있습니다")
        return v

    def default_blocks(self) -> list[str]:
        """blocks 를 적지 않은 (예전) 프로필의 구성. 자동 블록은 해당 없으면 비어서 빠진다."""
        return ["work_rules", *(["dev_standard"] if self.standards else []), "intranet_status", "environment",
                "lang_standard"]

    def effective_blocks(self) -> list[str]:
        return list(self.blocks) if self.blocks is not None else self.default_blocks()

    def to_yaml(self) -> str:
        from agent.prompts import dump_yaml
        data = self.model_dump()
        out = {k: data[k] for k in FIELD_ORDER if data.get(k) is not None and data.get(k) != {}}
        if out.get("system_prompt") and not out["system_prompt"].endswith("\n"):
            out["system_prompt"] += "\n"   # 여러 줄 블록(|)으로 저장되게
        return dump_yaml(out)


def load_profile(name: str, agents_dir: Path | None = None, scopes: list[str] | None = None) -> Profile:
    """scopes: 이 실행에 붙는 하네스들 (agent/harness.active — None = 공통만). 그 하네스들의 역할이 먼저."""
    if agents_dir is None and NAME_RE.match(name):   # 하네스 역할(마크다운)이 먼저
        from agent import harness
        if (found := harness.find_role(name, scopes)) is not None:
            return Profile.model_validate(harness.role_profile(name, found[1]))
    agents_dir = agents_dir or AGENTS_DIR
    path = agents_dir / f"{name}.yaml"
    if not NAME_RE.match(name) or not path.exists():
        raise FileNotFoundError(f"프로필이 없습니다: {path} (사용 가능: {', '.join(list_profiles(agents_dir, scopes))})")
    return Profile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def list_profiles(agents_dir: Path | None = None, scopes: list[str] | None = None) -> list[str]:
    names = {p.stem for p in (agents_dir or AGENTS_DIR).glob("*.yaml") if NAME_RE.match(p.stem)}
    if agents_dir is None:
        from agent import harness
        names |= {n for n in harness.all_role_names(scopes) if NAME_RE.match(n)}
    return sorted(names)


def harness_role(name: str) -> bool:
    """이 용도가 하네스 역할 파일(마크다운)에서 오는지 — 그러면 '설정'의 yaml 편집은 적용되지 않는다."""
    from agent import harness
    return harness.role_text(name) is not None


def profile_path(name: str, agents_dir: Path | None = None) -> Path:
    if not NAME_RE.match(name):
        raise ValueError("프로필 이름은 영어 소문자·숫자·-·_ 로 32자 이하 (첫 글자는 영어 소문자나 숫자)")
    return (agents_dir or AGENTS_DIR) / f"{name}.yaml"
