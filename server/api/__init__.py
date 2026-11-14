"""관리 서버 API v1. 화면(web/, React)은 이 API 만 쓴다.

    /api/v1/runs …          실행 (시작·조회·SSE·승인·중지)       server/api/runs.py
    /api/v1/profiles …      입력창 선택 목록 (용도·워크플로)      server/api/catalog.py
    /api/v1/status …        상태·모델 서버·모델 교체              server/api/system.py
    /api/v1/intranet …      사내 사이트·API·허용 주소·토큰        server/api/connections.py
    /api/v1/workspace …     작업 폴더·파일 보기 (읽기 전용)       server/api/files.py
    /api/v1/locations …     파일 탐색기·공유 위치 (읽기 전용)     server/api/locations.py
    /api/v1/schedules …     예약 실행 (내 것만 · 관리자는 전부)    server/api/schedules.py
    /api/v1/settings …      프롬프트·용도·워크플로·파라미터 …     server/api/settings/   (관리자)
    /api/v1/auth …          로그인·비밀번호·비밀번호 찾기         server/api/auth.py     (로그인 없이)
    /api/v1/me …            내 사내 API 계정                      server/api/auth.py
    /api/v1/admin …         사용자·회사·외부 모델·기록·접속 설정  server/api/admin.py    (관리자)
    /api/v1/harness …       하네스 (공통 지침·역할·자료 마크다운)  server/api/harness.py  (관리자)
    /api/v1/claude-code …   Claude Code 세션 매니저 (관리 탭)     server/api/claude_code.py (관리자)

'(관리자)' 라우터는 로그인 사용 모드에서 관리자만 부를 수 있다 (혼자 쓰기에서는 이 PC 사람 = 관리자).

계약(OpenAPI)은 /api/v1/openapi.json, 사람이 보는 문서는 /api/docs (인터넷이 되는 PC 에서).
web/openapi.json 은 이 계약을 저장한 것이고, 화면의 TypeScript 타입은 그 파일로 만든다 (npm run gen:api).
"""

from fastapi import APIRouter, Depends

from server import deps
from server.api import admin, auth, catalog, claude_code, connections, files, harness, locations, runs, schedules, system
from server.api import settings as settings_api

VERSION = "v1"
router = APIRouter(prefix=f"/api/{VERSION}")
for module in (runs, schedules, catalog, system, files, locations, auth, admin):
    router.include_router(module.router)
for module in (connections, settings_api, harness, claude_code):   # 모두가 같이 쓰는 설정·이 PC 의 Claude Code — 관리자만
    router.include_router(module.router, dependencies=[Depends(deps.require_admin)])
