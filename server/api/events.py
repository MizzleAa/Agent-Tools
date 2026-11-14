"""실행 이벤트의 API 모양 (agent/events.py 의 데이터클래스와 같은 필드).

화면은 이 이벤트를 받아 그리기만 한다: GET /runs/{id} 의 events, GET /runs/{id}/events (SSE) 의 data.
tests/test_api_contract.py 가 agent/events.py 와 필드가 같은지 확인한다.
예전 기록에 없는 필드가 있어도 읽히도록 모두 기본값을 둔다.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import ConfigDict, Field

from server.api.common import ApiModel


class _Event(ApiModel):
    model_config = ConfigDict(extra="allow")
    seq: int | None = None   # 실행 안에서의 순서 (SSE 재연결·중복 방지)


class RunStartedEvent(_Event):
    type: Literal["RunStarted"]
    run_id: str
    task: str
    profile: str
    model: str = ""
    parent_run_id: str = ""
    workdir: str = ""


class LLMCallEvent(_Event):
    type: Literal["LLMCall"]
    step: int = 0
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    finish_reason: str = Field(default="", description="모델 응답이 끝난 이유: stop · tool_calls · length")


class ToolCallEvent(_Event):
    type: Literal["ToolCall"]
    step: int = 0
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    permission: str = "unknown"


class ApprovalRequestedEvent(_Event):
    type: Literal["ApprovalRequested"]
    step: int = 0
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    always_ask: bool = False   # '전부 자동'이어도 사람에게 묻는 일 (되돌릴 수 없는 삭제 등)


class ApprovalResolvedEvent(_Event):
    type: Literal["ApprovalResolved"]
    call_id: str
    approved: bool


class ToolResultEvent(_Event):
    type: Literal["ToolResult"]
    step: int = 0
    call_id: str
    name: str
    ok: bool
    output: str = ""
    seconds: float = 0.0


class NoteEvent(_Event):
    type: Literal["Note"]
    step: int = 0
    kind: str
    message: str
    injected: str = ""


class WorkflowStartedEvent(_Event):
    type: Literal["WorkflowStarted"]
    name: str
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    input: str = ""


class NodeStartedEvent(_Event):
    type: Literal["NodeStarted"]
    node: str
    title: str
    profile: str
    index: int
    total: int
    task: str = ""


class NodeFinishedEvent(_Event):
    type: Literal["NodeFinished"]
    node: str
    title: str
    status: str
    answer: str = ""
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


class ImageItem(ApiModel):
    model_config = ConfigDict(extra="allow")
    id: int = Field(description="이 실행 안의 이미지 번호. 화면은 /api/v1/runs/{run_id}/images/{id}/{thumb|view|original} 로 받는다")
    api: str = ""
    path: str = Field(default="", description="이미지를 받은 API 경로 (인증 정보 없음)")
    label: str = Field(default="", description="이미지 아래 보일 이름 (파일명 등)")
    ok: bool = False
    reason: str = Field(default="", description="받지 못한 이유 (찾을 수 없음 · 권한 없음 · 이미지가 아님 …)")
    format: str = ""
    width: int = 0
    height: int = 0
    bytes: int = 0


class ImagesEvent(_Event):
    type: Literal["Images"]
    step: int = 0
    call_id: str = ""
    items: list[ImageItem] = Field(default_factory=list)


class ErrorEvent(_Event):
    type: Literal["Error"]
    message: str


class FinalEvent(_Event):
    type: Literal["Final"]
    status: str
    answer: str = ""
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    tools_used: list[str] = Field(default_factory=list)


RunEvent = Annotated[Union[
    RunStartedEvent, LLMCallEvent, ToolCallEvent, ApprovalRequestedEvent, ApprovalResolvedEvent, ToolResultEvent,
    NoteEvent, WorkflowStartedEvent, NodeStartedEvent, NodeFinishedEvent, ImagesEvent, ErrorEvent, FinalEvent,
], Field(discriminator="type")]

EVENT_MODELS = {m.model_fields["type"].annotation.__args__[0]: m for m in (  # type: ignore[union-attr]
    RunStartedEvent, LLMCallEvent, ToolCallEvent, ApprovalRequestedEvent, ApprovalResolvedEvent, ToolResultEvent,
    NoteEvent, WorkflowStartedEvent, NodeStartedEvent, NodeFinishedEvent, ImagesEvent, ErrorEvent, FinalEvent)}
