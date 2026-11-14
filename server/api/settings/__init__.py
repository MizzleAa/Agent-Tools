"""관리 화면 '설정' API (/api/v1/settings/...).

저장하는 파일
- 용도(프로필): config/agents/<이름>.yaml          · 워크플로: config/workflows/<이름>.yaml
- 블록·메시지·도구 설명·변수: config/prompts.yaml  · 실행 파라미터: config/settings.yaml (주석 유지)
- 모델 목록: config/connections.yaml               · 개발 규격: config/standards/*.md
저장할 때마다 바뀌기 전 내용을 data/settings_history/ 에 남긴다. 다음 실행부터 바로 쓰인다 (재시작 불필요).
"""

from fastapi import APIRouter

from server.api.common import ERRORS
from server.api.settings import files, models, profiles, prompts, workflows
from server.api.settings.schemas import SettingsBundle
from server.api.settings.views import bundle

router = APIRouter(prefix="/settings", tags=["settings"], responses=ERRORS)


@router.get("", response_model=SettingsBundle, summary="설정 화면에 필요한 것 한 번에")
def get_settings() -> SettingsBundle:
    return bundle()


for sub in (profiles, workflows, prompts, models, files):
    router.include_router(sub.router)
