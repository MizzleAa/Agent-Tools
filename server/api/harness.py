"""관리 › 하네스 (관리자만): 공통 지침(AGENT.md) · 역할(agents/*.md) · 자료 폴더(사용설명서 · 에러 대처 같은 QA 자료)를
마크다운 파일로 만들고 고친다. 폴더 AGENT.md 의 '대상 회사'로 그 폴더 자료를 쓸 회사를 정한다. 저장 위치는 harness/ (agent/harness.py).

하네스 여러 개: 회사·프로젝트 단위 하네스(harnesses/<이름>/)를 /harness/scopes 로 만들고, 다른 경로는 ?scope=<이름> 으로
그 하네스 안을 다룬다 (비우면 공통).
"""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field

from agent import harness
from agent.profiles import list_profiles, load_profile
from server.api.common import ApiModel, ERRORS, Ok

router = APIRouter(prefix="/harness", tags=["harness"], responses=ERRORS)


class HarnessFolder(ApiModel):
    path: str = Field(description="'' = 맨 위")
    companies: list[str] = Field(description="이 폴더 AGENT.md 에 적은 대상 회사")
    effective: list[str] = Field(description="위 폴더에서 물려받은 것까지 — 비면 모든 회사")
    has_instructions: bool


class HarnessDoc(ApiModel):
    path: str
    folder: str
    kind: str = Field(description="instructions(AGENT.md) · role(agents/*.md) · doc(자료)")
    title: str
    description: str
    tags: list[str]
    size: int
    modified: str
    error: str = Field(description="머리말(---) 문법 오류")
    scope: str = Field(description="어느 하네스 ('' = 공통)")


class HarnessTree(ApiModel):
    root: str = Field(description="하네스 폴더 실제 위치")
    scope: str = Field(description="'' = 공통, 그 밖에는 회사·프로젝트 하네스 이름")
    folders: list[HarnessFolder]
    files: list[HarnessDoc]
    yaml_roles: list[str] = Field(description="아직 하네스로 옮기지 않은 기존 용도(config/agents/*.yaml) — 공통에서만")


class HarnessScope(ApiModel):
    id: str = Field(description="이름 (영어 소문자·숫자·-·_) — 폴더 harnesses/<이름>")
    title: str
    kind: str = Field(description="company(회사) · project(프로젝트)")
    companies: list[str] = Field(description="대상 회사 (비면 모두). 회사 하네스는 그 회사 사람의 실행에 저절로 붙는다")
    description: str
    roles: list[str] = Field(description="이 하네스의 용도")


class ScopeCreate(ApiModel):
    id: str
    title: str = ""
    kind: Literal["company", "project"] = "project"
    companies: list[str] = Field(default_factory=list)
    description: str = ""


class ScopeUpdate(ApiModel):
    title: str | None = None
    kind: Literal["company", "project"] | None = None
    companies: list[str] | None = None
    description: str | None = None


class HarnessFile(ApiModel):
    path: str
    text: str


class HarnessFileOut(HarnessFile):
    meta: dict = Field(description="머리말을 읽은 값")
    error: str


class FolderBody(ApiModel):
    path: str


class CompaniesBody(ApiModel):
    path: str = Field(description="폴더 ('' = 맨 위 — 맨 위에 대상을 정하면 모든 자료가 그 회사 전용이 된다)")
    companies: list[str] = Field(default_factory=list)


class RenameBody(ApiModel):
    src: str
    dst: str


class UploadItem(ApiModel):
    name: str = Field(description="원래 파일 이름 (.md · .txt · .pdf · .docx · .xlsx · .csv)")
    data: str = Field(description="파일 내용 (base64)")


class UploadBody(ApiModel):
    folder: str = ""
    files: list[UploadItem]


class UploadResult(ApiModel):
    saved: list[HarnessDoc]
    failed: list[str]


class Exported(ApiModel):
    written: list[str]


SCOPE_Q = Query("", description="'' = 공통 하네스, 그 밖에는 회사·프로젝트 하네스 이름 (GET /harness/scopes)")


def _tree(scope: str = "") -> HarnessTree:
    _need(scope)
    folders, docs = harness.scan(scope)
    md_roles = set(harness.role_names())
    return HarnessTree(root=str(harness.root(scope)), scope=scope,
                       folders=[HarnessFolder(**x) for x in harness.asdicts(folders)],
                       files=[HarnessDoc(**x) for x in harness.asdicts(docs)],
                       yaml_roles=[] if scope else [n for n in list_profiles() if n not in md_roles])


def _bad(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))


def _need(scope: str) -> None:
    if scope and harness.get_scope(scope) is None:
        raise HTTPException(404, f"없는 하네스입니다: {scope}")


def _scope_out(sc: harness.Scope) -> HarnessScope:
    return HarnessScope(**harness.asdicts([sc])[0])


# ───────────────────────── 하네스 여러 개 (회사 · 프로젝트)

@router.get("/scopes", response_model=list[HarnessScope], summary="회사·프로젝트 하네스 목록 (공통은 빠짐)")
def scopes() -> list[HarnessScope]:
    return [_scope_out(sc) for sc in harness.list_scopes()]


@router.post("/scopes", response_model=HarnessScope, summary="회사·프로젝트 하네스 만들기")
def create_scope(body: ScopeCreate) -> HarnessScope:
    try:
        return _scope_out(harness.create_scope(body.id.strip(), body.title, body.kind, body.companies, body.description))
    except ValueError as e:
        raise _bad(e)


@router.put("/scopes/{scope_id}", response_model=HarnessScope, summary="하네스 이름·종류·대상 회사 바꾸기")
def update_scope(scope_id: str, body: ScopeUpdate) -> HarnessScope:
    _need(scope_id)
    try:
        return _scope_out(harness.update_scope(scope_id, body.title, body.kind, body.companies, body.description))
    except ValueError as e:
        raise _bad(e)


@router.delete("/scopes/{scope_id}", response_model=Ok, summary="하네스 지우기 (안의 지침·용도·자료까지)")
def delete_scope(scope_id: str) -> Ok:
    _need(scope_id)
    harness.delete_scope(scope_id)
    return Ok()


# ───────────────────────── 하네스 안 (scope = '' 공통)

@router.get("", response_model=HarnessTree, summary="하네스 폴더·파일 목록")
def tree(scope: str = SCOPE_Q) -> HarnessTree:
    return _tree(scope)


@router.get("/file", response_model=HarnessFileOut, summary="하네스 파일 읽기")
def read_file(path: str = Query(...), scope: str = SCOPE_Q) -> HarnessFileOut:
    _need(scope)
    try:
        p = harness.safe(path, scope=scope)
    except ValueError as e:
        raise _bad(e)
    if not p.is_file():
        raise HTTPException(404, f"없는 파일입니다: {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    meta, _ = harness.split_front(text)
    return HarnessFileOut(path=harness.rel_of(p, scope), text=text, meta=meta, error=harness.check(text))


@router.put("/file", response_model=HarnessDoc, summary="하네스 파일 저장 (없으면 만든다)")
def write_file(body: HarnessFile, scope: str = SCOPE_Q) -> HarnessDoc:
    _need(scope)
    try:
        return HarnessDoc(**harness.asdicts([harness.write(body.path, body.text, scope)])[0])
    except ValueError as e:
        raise _bad(e)


@router.delete("/file", response_model=Ok, summary="하네스 파일·폴더 지우기 (폴더면 안의 것까지)")
def delete_file(path: str = Query(...), scope: str = SCOPE_Q) -> Ok:
    _need(scope)
    try:
        harness.delete(path, scope)
    except ValueError as e:
        raise _bad(e)
    return Ok()


@router.post("/folders", response_model=HarnessFolder, summary="자료 폴더 만들기")
def make_folder(body: FolderBody, scope: str = SCOPE_Q) -> HarnessFolder:
    _need(scope)
    try:
        return HarnessFolder(**harness.asdicts([harness.mkdir(body.path, scope)])[0])
    except ValueError as e:
        raise _bad(e)


@router.put("/folders/companies", response_model=HarnessFolder, summary="폴더의 대상 회사 정하기 (빈 목록 = 모두)")
def set_companies(body: CompaniesBody, scope: str = SCOPE_Q) -> HarnessFolder:
    _need(scope)
    try:
        return HarnessFolder(**harness.asdicts([harness.set_companies(body.path, body.companies, scope)])[0])
    except ValueError as e:
        raise _bad(e)


@router.post("/rename", response_model=Ok, summary="하네스 파일·폴더 이름 바꾸기 · 옮기기")
def rename(body: RenameBody, scope: str = SCOPE_Q) -> Ok:
    _need(scope)
    try:
        harness.rename(body.src, body.dst, scope)
    except ValueError as e:
        raise _bad(e)
    return Ok()


@router.post("/upload", response_model=UploadResult, summary="자료 올리기 (md·txt 그대로, PDF·워드·엑셀·CSV 는 글로 바꿔 마크다운으로)")
def upload(body: UploadBody, scope: str = SCOPE_Q) -> UploadResult:
    _need(scope)
    saved, failed = [], []
    for item in body.files:
        try:
            data = base64.b64decode(item.data, validate=True)
            saved.append(HarnessDoc(**harness.asdicts([harness.import_file(body.folder, item.name, data, scope)])[0]))
        except (ValueError, binascii.Error) as e:
            failed.append(f"{item.name}: {e}")
    return UploadResult(saved=saved, failed=failed)


@router.post("/export-roles", response_model=Exported, summary="기존 용도(config/agents/*.yaml)를 하네스 역할 마크다운으로 옮기기")
def export_roles() -> Exported:
    written = []
    for name in _tree().yaml_roles:
        try:
            text = harness.role_markdown(load_profile(name))
            harness.write(f"{harness.ROLES}/{name}.md", text)
            written.append(f"{harness.ROLES}/{name}.md")
        except (ValueError, FileNotFoundError):
            continue
    return Exported(written=written)
