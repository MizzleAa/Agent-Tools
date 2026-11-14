"""용도(프로필): 만들기 · 고치기 · 지우기 · 기본값으로 · 미리보기. 파일은 config/agents/<이름>.yaml."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from agent import history, prompt_preview, prompts
from agent.profiles import Profile, profile_path
from agent.tools import REGISTRY
from server import deps
from server.api.common import ERRORS, Ok, bad_request
from server.api.settings import schemas as sc
from server.api.settings.views import profile_entry

router = APIRouter(responses=ERRORS)


def _check(p: Profile) -> None:
    try:
        profile_path(p.name)
    except ValueError as e:
        raise bad_request(e)
    unknown = [t for t in p.tools if t not in REGISTRY]
    if unknown:
        raise HTTPException(400, f"없는 도구: {', '.join(unknown)}")
    if not p.system_prompt.strip():
        raise HTTPException(400, "역할 프롬프트가 비어 있습니다")
    if any(not prompts.NAME_RE.match(k) or k in prompts.BUILTIN_VARS for k in p.variables):
        raise HTTPException(400, "변수 이름은 영어 소문자·숫자·_ 만 쓰고, 내장 변수 이름(cwd, today …)은 쓸 수 없습니다")


def _write(p: Profile, note: str) -> sc.ProfileEntry:
    history.write(profile_path(p.name), p.to_yaml(), deps.load_settings().data_path, note=note)
    return profile_entry(p.name)


@router.post("/profiles", response_model=sc.ProfileEntry, status_code=201, summary="용도 만들기")
def create_profile(body: Profile) -> sc.ProfileEntry:
    _check(body)
    if profile_path(body.name).exists():
        raise HTTPException(409, f"'{body.name}' 프로필이 이미 있습니다")
    return _write(body, f"새 프로필 {body.name}")


@router.put("/profiles/{name}", response_model=sc.ProfileEntry, summary="용도 저장")
def update_profile(name: str, body: Profile) -> sc.ProfileEntry:
    if body.name != name:
        raise HTTPException(400, "주소의 이름과 내용의 name 이 다릅니다 (이름은 바꿀 수 없습니다. 복제해서 새로 만드세요)")
    _check(body)
    if not profile_path(name).exists():
        raise HTTPException(404, f"'{name}' 프로필이 없습니다")
    return _write(body, f"프로필 저장 {name}")


@router.delete("/profiles/{name}", response_model=Ok, summary="용도 지우기 (기본 제공은 못 지움)")
def delete_profile(name: str) -> Ok:
    try:
        path = profile_path(name)
    except ValueError as e:
        raise bad_request(e)
    if not path.exists():
        raise HTTPException(404, "프로필이 없습니다")
    if (prompts.DEFAULT_AGENTS_DIR / path.name).exists():
        raise HTTPException(400, "기본 제공 프로필은 지울 수 없습니다. 내용을 되돌리려면 '기본값으로'를 누르세요")
    history.write(path, None, deps.load_settings().data_path, note=f"프로필 삭제 {name}")
    return Ok()


@router.post("/profiles/{name}/reset", response_model=sc.ProfileEntry, summary="기본 제공 용도를 처음 상태로")
def reset_profile(name: str) -> sc.ProfileEntry:
    default = prompts.DEFAULT_AGENTS_DIR / f"{name}.yaml"
    if not default.exists():
        raise HTTPException(400, "기본 제공 프로필이 아니라 되돌릴 기본값이 없습니다 (변경 기록에서 되돌릴 수 있습니다)")
    history.write(profile_path(name), default.read_text(encoding="utf-8"), deps.load_settings().data_path,
                  note=f"프로필 기본값으로 {name}")
    return profile_entry(name)


@router.post("/preview", response_model=sc.PromptPreview, summary="저장하지 않은 용도로 조립 결과·토큰·점검 보기")
def preview(body: Profile) -> dict:
    _check(body)
    try:
        return prompt_preview.preview(deps.load_settings(), body)
    except ValueError as e:
        raise bad_request(e)
