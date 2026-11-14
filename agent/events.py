"""에이전트 루프가 내보내는 이벤트.

loop.run() 은 아래 이벤트를 순서대로 yield 한다. CLI 출력, DB 저장, 웹 UI 스트리밍은
모두 이 이벤트만 보고 동작하므로, 에이전트 로직은 loop.py 한 곳에만 있으면 된다.

    RunStarted → (LLMCall → [ToolCall → (ApprovalRequested → ApprovalResolved)? → ToolResult]*)* → Final

워크플로(agent/workflow.py)는 노드마다 위 흐름을 감싸서 실행 하나로 내보낸다.

    RunStarted → WorkflowStarted → (NodeStarted → [노드 안의 LLMCall·ToolCall…] → NodeFinished)* → Final
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Event:
    def to_dict(self) -> dict[str, Any]:
        return {"type": type(self).__name__, **asdict(self)}


@dataclass
class RunStarted(Event):
    run_id: str
    task: str
    profile: str
    model: str
    parent_run_id: str = ""   # '이어서 하기'로 시작했다면 이전 실행 id
    workdir: str = ""         # 이 대화(세션) 전용 작업 폴더


@dataclass
class LLMCall(Event):
    """모델 한 번 호출의 결과. tool_calls 가 비어 있으면 최종 답변이라는 뜻."""
    step: int
    content: str
    reasoning: str
    tool_calls: list[dict[str, Any]]
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    finish_reason: str = ""   # stop · tool_calls · length (끊긴 답을 가려내는 데 씀)


@dataclass
class ToolCall(Event):
    step: int
    call_id: str
    name: str
    arguments: dict[str, Any]
    permission: str


@dataclass
class ApprovalRequested(Event):
    step: int
    call_id: str
    name: str
    arguments: dict[str, Any]
    reason: str
    always_ask: bool = False   # 되돌릴 수 없는 일(작업·프로젝트 폴더 통째 삭제 등): '전부 자동'이어도 사람에게 묻는다


@dataclass
class ApprovalResolved(Event):
    call_id: str
    approved: bool


@dataclass
class ToolResult(Event):
    step: int
    call_id: str
    name: str
    ok: bool
    output: str
    seconds: float


@dataclass
class Note(Event):
    """루프가 끼어든 일. kind: act(예고만 해서 되돌려 보냄) · gate(규격 점검) · standard(언어 규격 붙임) · failures(같은 실패 반복)
    injected 가 있으면 그 말을 모델에게 사용자 메시지로 넣었다는 뜻이다."""
    step: int
    kind: str
    message: str
    injected: str = ""


@dataclass
class WorkflowStarted(Event):
    """워크플로 실행 (agent/workflow.py). RunStarted 바로 뒤에 한 번. nodes: [{id, title, profile, after}]"""
    name: str
    nodes: list[dict[str, Any]]
    input: str = ""   # {{input}} 값 (이어서 하기에서도 처음 요청이 유지된다)


@dataclass
class NodeStarted(Event):
    """워크플로의 노드 하나가 시작됨. 이 뒤의 LLMCall·ToolCall… 은 NodeFinished 까지 이 노드의 것이다."""
    node: str
    title: str
    profile: str
    index: int
    total: int
    task: str


@dataclass
class NodeFinished(Event):
    """status: Final 과 같은 값 + skipped(앞 노드가 실패해 건너뜀) · reused(이어서 하기에서 이전 결과를 그대로 씀)"""
    node: str
    title: str
    status: str
    answer: str
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


@dataclass
class Images(Event):
    """사내 API 에서 받아 화면에 붙인 이미지 (show_images). 이미지 바이트는 실행 폴더에만 있고 모델·이벤트에는 없다.
    items: [{id, api, path, label, ok, reason, format, width, height, bytes}] — 화면은 /runs/{run}/images/{id}/thumb 로 받는다."""
    step: int
    call_id: str
    items: list[dict[str, Any]]


@dataclass
class Error(Event):
    message: str


@dataclass
class Final(Event):
    """status: done | partial(부분 완료) | failed(보고상 실패) | no_action(실행하지 않고 예고만 함)
            | max_steps | timeout | loop(반복 감지) | cancelled | error"""
    status: str
    answer: str
    steps: int
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    tools_used: list[str] = field(default_factory=list)
