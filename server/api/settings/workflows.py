"""워크플로: 만들기 · 고치기 · 지우기 · 기본값으로. 파일은 config/workflows/<이름>.yaml."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from agent import history
from agent import workflow as workflows
from server import deps
from server.api.common import ERRORS, Ok, bad_request
from server.api.settings import schemas as sc
from server.api.settings.views import workflow_entry

router = APIRouter(responses=ERRORS)


def _check(wf: workflows.Workflow):
    try:
        path = workflows.workflow_path(wf.name)
        workflows.WorkflowRunner(None, wf)   # 노드의 프로필이 모두 있는지
    except ValueError as e:
        raise bad_request(e)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    return path


@router.post("/workflows", response_model=sc.WorkflowEntry, status_code=201, summary="워크플로 만들기")
def create_workflow(body: workflows.Workflow) -> sc.WorkflowEntry:
    path = _check(body)
    if path.exists():
        raise HTTPException(409, f"'{body.name}' 워크플로가 이미 있습니다")
    history.write(path, body.to_yaml(), deps.load_settings().data_path, note=f"새 워크플로 {body.name}")
    return workflow_entry(body.name)


@router.put("/workflows/{name}", response_model=sc.WorkflowEntry, summary="워크플로 저장")
def update_workflow(name: str, body: workflows.Workflow) -> sc.WorkflowEntry:
    if body.name != name:
        raise HTTPException(400, "주소의 이름과 내용의 name 이 다릅니다 (이름은 바꿀 수 없습니다. 복제해서 새로 만드세요)")
    path = _check(body)
    if not path.exists():
        raise HTTPException(404, f"'{name}' 워크플로가 없습니다")
    history.write(path, body.to_yaml(), deps.load_settings().data_path, note=f"워크플로 저장 {name}")
    return workflow_entry(name)


@router.delete("/workflows/{name}", response_model=Ok, summary="워크플로 지우기")
def delete_workflow(name: str) -> Ok:
    try:
        path = workflows.workflow_path(name)
    except ValueError as e:
        raise bad_request(e)
    if not path.exists():
        raise HTTPException(404, "워크플로가 없습니다")
    history.write(path, None, deps.load_settings().data_path, note=f"워크플로 삭제 {name}")
    return Ok()


@router.post("/workflows/{name}/reset", response_model=sc.WorkflowEntry, summary="기본 제공 워크플로를 처음 상태로")
def reset_workflow(name: str) -> sc.WorkflowEntry:
    default = workflows.DEFAULT_DIR / f"{name}.yaml"
    if not default.exists():
        raise HTTPException(400, "기본 제공 워크플로가 아닙니다 (변경 기록에서 되돌릴 수 있습니다)")
    history.write(workflows.workflow_path(name), default.read_text(encoding="utf-8"), deps.load_settings().data_path,
                  note=f"워크플로 기본값으로 {name}")
    return workflow_entry(name)
