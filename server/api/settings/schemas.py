"""'설정' API 의 요청·응답 모양. 용도(Profile)·워크플로(Workflow)는 agent 의 모델을 그대로 쓴다 (검증 규칙도 같이)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from agent.profiles import Profile
from agent.workflow import Workflow
from server.api.common import ApiModel


# ───────────────────────── 용도(프로필)

class ProfileEntry(ApiModel):
    name: str
    file: str = Field(description="config 기준 파일 경로")
    builtin: bool = Field(description="기본 제공 프로필 ('기본값으로' 가능)")
    modified: bool | None = Field(default=None, description="기본값과 다른지 (기본 제공일 때만)")
    error: str | None = Field(default=None, description="파일을 읽지 못했을 때 이유 (그때 profile 은 없음)")
    harness: bool = Field(default=False, description="하네스 역할 파일(harness/agents/<이름>.md)에서 오는 용도 — 거기서 고친다")
    profile: Profile | None = None


# ───────────────────────── 프롬프트 조각

class BlockInfo(ApiModel):
    id: str
    title: str
    description: str
    text: str
    default: str | None = Field(description="기본 제공 블록의 원래 내용 (직접 만든 블록·자동 블록이면 없음)")
    dynamic: bool = Field(description="프로그램이 내용을 채우는 블록 (사내 연결 현황, 언어별 규격)")
    custom: bool
    modified: bool
    used_by: list[str]


class BlockWrite(ApiModel):
    title: str = ""
    description: str = ""
    text: str


class MessageInfo(ApiModel):
    key: str
    title: str
    when: str
    vars: dict[str, str]
    text: str
    default: str
    modified: bool


class TextWrite(ApiModel):
    text: str


class ToolInfo(ApiModel):
    name: str
    permission: Literal["read", "write", "exec", "send", "admin"]
    default: str = Field(description="코드(docstring)의 설명")
    text: str = Field(description="지금 모델이 보는 설명")
    modified: bool
    params: dict[str, Any] = Field(description="인자 JSON 스키마 (코드에서 정해짐)")
    used_by: list[str]


class VariableInfo(ApiModel):
    name: str
    value: str
    description: str
    default: str | None
    custom: bool
    modified: bool


class VariableWrite(ApiModel):
    value: str = ""
    description: str = ""


class BuiltinVar(ApiModel):
    name: str
    description: str


# ───────────────────────── 실행 파라미터 (settings.yaml)

class ParamField(ApiModel):
    key: str
    label: str
    type: Literal["float", "int", "bool", "str", "list", "select"]
    help: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list[str] | None = None
    nullable: bool = False


class ParamSection(ApiModel):
    title: str
    help: str
    fields: list[ParamField]


class ParamsInfo(ApiModel):
    sections: dict[str, ParamSection]
    file: dict[str, dict[str, Any]] = Field(description="settings.yaml 에 실제로 적힌 값")
    effective: dict[str, dict[str, Any]] = Field(description="지금 적용되는 값 (모델 목록 설정 등이 반영됨)")
    defaults: dict[str, dict[str, Any]] = Field(description="settings.yaml 에 없을 때의 값")
    active_model: str


class ParamsWrite(ApiModel):
    values: dict[str, Any] = Field(description="바꿀 키만. nullable 항목에 null 을 주면 그 키를 지운다")


class ParamsSaved(ApiModel):
    file: dict[str, Any]


# ───────────────────────── 개발 규격 · 변경 기록

class StandardFile(ApiModel):
    name: str
    chars: int


class StandardText(ApiModel):
    name: str
    text: str


class HistoryEntry(ApiModel):
    id: str
    target: str = Field(description="config 기준 파일 경로")
    time: str
    note: str
    existed: bool = Field(description="저장 직전에 파일이 있었는지 (없었으면 되돌리기 = 파일 지우기)")


class HistoryDetail(HistoryEntry):
    text: str = Field(description="저장 직전 내용")
    current: str = Field(description="지금 내용")
    diff: str = Field(description="그때 → 지금 unified diff")


class RestoreResult(ApiModel):
    target: str


# ───────────────────────── 워크플로

class WorkflowEntry(ApiModel):
    name: str
    file: str
    builtin: bool
    modified: bool | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list, description="저장은 됐지만 확인할 것 (없는 변수, 뒤 노드 참조 …)")
    workflow: Workflow | None = None


# ───────────────────────── 모델 목록

class ModelPresetInfo(ApiModel):
    name: str
    file: str
    note: str
    context_length: int | None
    gpu_layers: int | None
    extra_args: list[str]
    managed: bool = Field(description="설정 화면에서 추가한 모델 (목록에서 뺄 수 있음)")
    overridden: bool = Field(description="settings.yaml 의 모델인데 화면에서 값을 바꿈")
    base: dict[str, Any] | None = Field(description="settings.yaml 원래 값 (화면에서 추가한 모델이면 없음)")
    state: Literal["ready", "downloading", "missing"]
    active: bool
    size_gb: float | None


class ModelFile(ApiModel):
    file: str
    size_gb: float
    state: Literal["ready", "downloading", "missing"]
    suggested_name: str


class ModelDefaults(ApiModel):
    context_length: int
    gpu_layers: int


class ModelCreate(ApiModel):
    name: str
    file: str = Field(description="GGUF 경로 (프로젝트 기준 상대 경로 또는 절대 경로)")
    note: str = ""
    context_length: int | None = None
    gpu_layers: int | None = None
    extra_args: str | list[str] = Field(default="", description="예) --n-cpu-moe 36")


class ModelUpdate(ApiModel):
    note: str | None = None
    context_length: int | None = None
    gpu_layers: int | None = None
    extra_args: str | list[str] | None = None


class ModelSaved(ApiModel):
    model: ModelPresetInfo
    restart_needed: bool = Field(description="지금 쓰는 모델이라 모델 서버를 다시 켜야 적용됨")


class RestartNeeded(ApiModel):
    restart_needed: bool


# ───────────────────────── 미리보기

class PreviewSection(ApiModel):
    id: str
    title: str
    text: str
    chars: int
    tokens: int


class PreviewTools(ApiModel):
    count: int
    tokens: int
    chars: int
    json_text: str = Field(alias="json", description="모델이 보는 도구 설명 (한 줄에 도구 하나)")


class LintItem(ApiModel):
    level: Literal["warn", "info"]
    text: str


class PromptPreview(ApiModel):
    prompt: str
    sections: list[PreviewSection]
    tools: PreviewTools
    system_tokens: int
    total_tokens: int
    exact: bool = Field(description="모델 서버 /tokenize 로 정확히 셌는지 (아니면 어림)")
    context_length: int
    budget: int = Field(description="컨텍스트에서 답변 여유를 뺀 예산")
    lang_extra: dict[str, int] = Field(description="개발 언어를 알아채면 붙는 언어별 규격의 토큰 수")
    warnings: list[LintItem]
    model: str


# ───────────────────────── 한 번에 (설정 화면을 열 때)

class HarnessInfo(ApiModel):
    id: str = Field(description="회사·프로젝트 하네스 이름 (harnesses/<이름>)")
    title: str
    kind: str = Field(description="company(회사) · project(프로젝트)")
    companies: list[str]
    roles: list[str] = Field(description="이 하네스의 용도 — 워크플로 노드에서 이 하네스를 고르면 용도 목록에 더해진다")


class SettingsBundle(ApiModel):
    profiles: list[ProfileEntry]
    workflows: list[WorkflowEntry]
    harnesses: list[HarnessInfo]
    blocks: list[BlockInfo]
    messages: list[MessageInfo]
    tools: list[ToolInfo]
    variables: list[VariableInfo]
    builtin_vars: list[BuiltinVar]
    params: ParamsInfo
    standards: list[StandardFile]
    models: list[ModelPresetInfo]
    model_files: list[ModelFile]
    model_defaults: ModelDefaults
    max_tools: int
    max_nodes: int
    context_length: int
