# Agent — 개발 세션 안내 (Claude Code)

이 파일은 이 폴더(C:\workspace\Agent-Tools)를 열어 개발하는 Claude Code 세션용이다.
이 프로그램이 띄우는 CLI 대화(작업 폴더 workspace\… 아래)에는 붙지 않는다 — `agent/claude_code/instructions.py` 의
`dev_md_excludes()` 가 claudeMdExcludes 로 뺀다. 그러니 여기에는 사용자 대화용 지침을 쓰지 않는다.

## 세션을 시작하면 먼저 (세션이 끊겼다 다시 켜진 경우 포함)

1. `.claude/work/progress.md` 를 읽는다 — 진행 중인 개발 작업의 체크리스트다.
2. `[ ]` 가 남아 있으면 사용자에게 한 줄로 알리고 그다음 항목부터 이어서 한다. 끝낸 항목은 바로 `[x]` 로 고친다.
3. 남은 일이 있으면 주기 점검을 다시 건다 (예약 작업은 세션과 함께 사라진다):
   CronCreate — 30분쯤마다(정각·30분 피해서) "`.claude/work/progress.md` 를 보고 남은 일을 이어서, 다 끝나면 CronDelete".
4. 다 끝났으면 progress.md 맨 위에 끝난 날짜를 적고 주기 점검을 지운다.

`.claude/work/` 의 다른 파일은 그 작업의 조사 기록이다 (예: `codex_appserver.md` · `gemini_acp.md` — CLI 프로토콜).

## 지켜야 할 것

- 승인 절차를 우회하지 않는다 (bypassPermissions · 자동 승인 정책 금지). Claude Code 기록 파일은 읽기만.
- 테스트는 진짜 데이터를 건드리지 않는다 (tests/conftest.py 가 계정·하네스·CLI 를 임시로 바꿔 끼운다).
- API 를 바꾸면 `scripts/export_openapi.py` → web 에서 `npm run gen:api` (tests/test_api_contract.py 가 확인).
- 관리 서버 재시작은 진행 중인 실행·CLI 세션이 없는지 확인한 뒤, 작업 스케줄러 1회 작업(숨긴 창)으로.
- 백슬래시가 든 코드를 셸 heredoc 으로 고치지 않는다 — Edit 도구나 raw 문자열 스크립트로.
- 사용자 말투·용어: 한국어, '용도'(프로필) · '하네스' · 'API' · 'CLI'. 새 기능은 한 화면(대화 화면 모델 선택)에 넣는다.
