"""입력창에서 고르는 것: 하네스 목록(과 그 용도), 용도(프로필) 목록, 워크플로 목록, 하네스 명령('/' 목록)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import Field

from agent import harness
from agent.profiles import list_profiles, load_profile
from agent.workflow import list_workflows, load_workflow
from server import deps
from server.api.common import ApiModel, ERRORS

router = APIRouter(tags=["catalog"], responses=ERRORS)


class ProfileSummary(ApiModel):
    name: str
    description: str
    tools: list[str]


class HarnessChoice(ApiModel):
    id: str
    title: str
    kind: str = Field(description="company(회사) · project(프로젝트)")
    description: str
    auto: bool = Field(description="이 사람의 회사가 대상인 회사 하네스 — 고르지 않아도 늘 붙는다")
    roles: list[ProfileSummary] = Field(description="이 하네스의 용도 (고르면 입력창 용도 목록에 더해진다)")


class HarnessCommand(ApiModel):
    name: str = Field(description="'/' 뒤 이름 (파일 이름)")
    description: str
    hint: str = Field("", description="인자 힌트 (머리말 argument-hint)")
    body: str = Field(description="보낼 요청 — '/이름' 뒤에 친 글이 $ARGUMENTS 자리에 들어간다 (없으면 끝에 붙는다)")
    scope: str = Field(description="어느 하네스 ('' = 공통)")


class WorkflowNodeSummary(ApiModel):
    id: str
    title: str
    harness: str
    profile: str
    after: list[str]


class WorkflowSummary(ApiModel):
    name: str
    description: str
    nodes: list[WorkflowNodeSummary]


@router.get("/profiles", response_model=list[ProfileSummary], summary="용도(프로필) 목록 (로그인 사용: 회사에 허용된 것만)")
def profiles() -> list[ProfileSummary]:
    allowed = deps.allowed_profiles(deps.user())
    out = []
    for name in list_profiles():
        if allowed is not None and name not in allowed:
            continue
        try:
            p = load_profile(name)
        except Exception:   # 손으로 고치다 깨진 프로필 하나 때문에 화면 전체가 안 뜨면 안 된다 ('설정'에서 오류를 보여 준다)
            continue
        out.append(ProfileSummary(name=p.name, description=p.description, tools=p.tools))
    return out


@router.get("/harnesses", response_model=list[HarnessChoice], summary="고를 수 있는 하네스와 그 용도 (로그인 사용: 대상 회사에 맞는 것만)")
def harnesses() -> list[HarnessChoice]:
    audience = deps.settings_for(deps.user()).audience
    auto = set(harness.active(audience))
    out = []
    for sc in harness.list_scopes():
        if not harness.usable(sc.id, audience):
            continue
        roles = []
        for name in sc.roles:
            try:
                p = load_profile(name, scopes=["", sc.id])
            except Exception:   # 깨진 역할 파일 하나 때문에 목록이 안 뜨면 안 된다 (하네스 화면에서 오류를 보여 준다)
                continue
            roles.append(ProfileSummary(name=p.name, description=p.description, tools=p.tools))
        out.append(HarnessChoice(id=sc.id, title=sc.title, kind=sc.kind, description=sc.description,
                                 auto=sc.id in auto, roles=roles))
    return out


@router.get("/commands", response_model=list[HarnessCommand],
            summary="하네스 명령 (commands/*.md) — 입력창 '/' 목록. 공통 + 이 사람이 쓸 수 있는 하네스 전부 (화면이 붙는 하네스 것만 쓴다)")
def commands() -> list[HarnessCommand]:
    audience = deps.settings_for(deps.user()).audience
    scopes = ["", *(sc.id for sc in harness.list_scopes() if harness.usable(sc.id, audience))]
    return [HarnessCommand(**c) for c in harness.commands(audience, scopes)]


@router.get("/workflows", response_model=list[WorkflowSummary], summary="워크플로 목록 (로그인 사용: 노드 용도가 모두 허용된 것만)")
def workflows() -> list[WorkflowSummary]:
    allowed = deps.allowed_profiles(deps.user())
    audience = deps.settings_for(deps.user()).audience
    out = []
    for name in list_workflows():
        try:
            wf = load_workflow(name)
        except Exception:
            continue
        if any(n.harness and not harness.usable(n.harness, audience) for n in wf.nodes):
            continue
        ok = None if allowed is None else allowed | {r for n in wf.nodes if n.harness for r in harness.role_names(n.harness)} \
            | {r for sc in harness.active(audience) if sc for r in harness.role_names(sc)}
        if ok is not None and any(n["profile"] not in ok for n in wf.summary()):
            continue
        out.append(WorkflowSummary(name=wf.name, description=wf.description,
                                   nodes=[WorkflowNodeSummary(**n) for n in wf.summary()]))
    return out
