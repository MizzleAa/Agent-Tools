# 진행 중인 개발 작업 (세션이 끊겨도 이어서 하기 — CLAUDE.md 가 이 파일을 가리킨다)

마지막 갱신: 2026-11-12 19:35 — 이번 요청(1·2·3·4)은 모두 끝남. 남은 것: 사용자가 Codex·Gemini 로그인 뒤 실제 모델로 한 번 써 보기.

## 요청 (2026-11-12 사용자)
1. 하네스를 기업·프로젝트 단위로 여러 개 + 옆에 '＋ 새 용도' — 하네스 + 용도 기준으로 워크플로를 만든다
2. '용도별' → '용도' (끝남)
3. CLI 에 Claude 말고 Gemini · OpenAI(Codex) 추가
4. 세션이 끊겨도 이어서 하게: CLAUDE.md + .claude/ (이 파일)

## 1. 하네스 여러 개 — 코드 끝, 화면 확인 남음
- [x] agent/harness.py — scope(harnesses/<id>), active(), list/create/update/delete_scope, find_role, 자료 경로 '@id/경로'
- [x] config.Settings.harness, loop.py, tools/knowledge.py, profiles.py(scopes), workflow.py(node.harness + lint)
- [x] server/api/harness.py (?scope=, /harness/scopes), catalog.py (GET /harnesses), runs.py (RunCreate.harness), settings bundle.harnesses
- [x] CLI: instructions.build(harness_id), manager(add_dirs, sdk_agents scopes), store 칸 harness, API harness 필드
- [x] 화면: 설정 › 하네스 [📁 공통][🏢 회사][📂 프로젝트][＋ 새 하네스] · 하네스 안 '용도 ＋ 새 용도' · 워크플로 노드 하네스 선택
- [x] 화면: 입력창 순서 모델 · 추론 · 하네스 · 용도 · 위치 · 승인 (로컬·API·CLI 같게)
- [x] tests/test_harness_scopes.py

## 3. CLI 엔진 Codex · Gemini — 백엔드 끝, 화면 남음
- [x] 조사: .claude/work/codex_appserver.md · .claude/work/gemini_acp.md
- [x] agent/claude_code/engines.py (JsonRpcProcess · locate · Installer · LoginFlows · CodexDriver · GeminiDriver), gemini_login_url.mjs
- [x] manager.py: Runner.engine/driver · _start_engine · _EngineSink · _engine_ask(acceptEdits·plan) · _engine_result · history_page
- [x] store.py: sessions.engine 칸 · engine_messages 표 / service.py engine 인자 / settings codex_path · gemini_path
- [x] server/api/claude_code.py: engine 필드 · /status engines[] · POST /engines/{engine}/login · /install
- [x] 개발용 CLAUDE.md 가 CLI 대화에 새지 않게 claudeMdExcludes (실제 CLI 로 확인함 — 없으면 부모 CLAUDE.md 를 읽는다)
- [x] tests/test_cli_engines.py (가짜 CLI tests/fake_engine_cli.py), conftest 가 진짜 CLI 를 찾지 않게
- [x] 계약 export(scripts/export_openapi.py) + web `npm run gen:api`
- [x] 화면 lib.ts: 모델 선택 값 'cc:<모델>'(Claude) · 'cx:<모델>'(Codex) · 'gm:<모델>'(Gemini) 풀이 함수, isCc 가 셋 다
- [x] 화면 Composer ModelSelect: CLI 묶음 이름 'CLI (외부 · 온라인)' + ⌘ Claude Code · ⌘ Codex · ⌘ Gemini (모델은 status.engines[].models), 엔진별 문제 표시
- [x] 화면 AppState ccReady 엔진별 · CcChatView: engine 넘기기(startAuto), 대화 중 다른 엔진 고르면 새 대화, 엔진 이름 표시, Gemini 는 추론 선택 숨김, 권한 모드 설명
- [x] 화면 ToolCard: 'Patch'(Codex diff — changes[{path,kind,diff}]) 그리기
- [x] 화면 CcConnection(관리 › 외부 AI 모델 › CLI): Codex · Gemini 칸 — 설치 상태·경로·버전·로그인(버튼 → 받은 url 을 새 창으로)·설치 버튼·상태 새로고침
- [x] 사용자에게 알릴 것: Codex 는 '로그인됨'으로 보이지만 토큰 만료 → CLI 탭에서 다시 로그인 필요. Gemini 도 첫 로그인 필요

## 마무리
- [x] 전체 pytest(661 통과) · web vitest · npm run build
- [x] 임시 서버(8766)로 화면 확인 (스크래치 agent_test_server.py 방식 — 끝나면 프로세스 정리)
- [x] 진행 중 실행·CLI 세션이 없는지 확인 후 관리 서버 재시작 (19:28) (메모리 server-restart-detached)
- [x] README · docs/CLI_ClaudeCode_완료보고.md · 메모리 갱신, 사용자에게 바꾼 파일 보고
- [x] 다 끝나면 주기 점검 cron 지우기 (CronList → CronDelete)

---

# 사용자 설명서 (2026-11-13 요청: "사용자 설명서 캡쳐 다 찍어서 마크다운으로")

결과: `docs/사용자설명서.md` + 캡처 `docs/images/manual/NN_이름.png`. 캡처는 임시 데이터 시험 서버(8766, 로그인 모드, 관리자 admin)로 —
실제 서버 · 계정 · 기록은 쓰지 않는다. 시험 서버 스크립트: 세션 scratchpad 의 agent_test_server.py (MANUAL_LOGIN=1, 데이터 폴더 인자).

- [x] 시험 서버 준비 (새 임시 데이터 폴더, 하네스 예시 파일, 로그인 모드)
- [x] 캡처: 첫 관리자 만들기 · 로그인
- [x] 캡처: 대화 화면(새 작업 · 입력창) · 로컬 모델 작업 진행/승인/결과 · 지침 보기
- [x] 캡처: CLI 대화(승인 패널 · 결과)
- [x] 캡처: 작업 폴더 · 코드 / 파일 탐색기 / 상태 · 연결
- [x] 캡처: 설정 · 프롬프트 (하네스 공통 · 프로젝트 하네스 · 용도 · 워크플로 · 공통 탭 몇 개)
- [x] 캡처: 관리 (사용자 · 회사 / 외부 AI 모델 API · CLI / 기록 · 사용량 / 접속 설정) · 내 계정
- [x] (고친 것: 입력창 줄바꿈, 대화 칸 가로 넘침 .thread/.assistant/.tool-body/.wf-body minmax — app.css)
- [x] docs/사용자설명서.md 쓰기 (화면별 설명 + 캡처), README 에 링크
- [x] 시험 서버 · 남은 프로세스 정리

---

# 설명서 8장(하네스 · 설정 · 프롬프트) 자세히 (2026-11-13 요청: "하네스 쓰는거랑 설정 프롬프트 쪽을 자세하게")

- [x] 조사: 설정 화면 · 지침 조립 순서 · 하네스 세부 (Explore 에이전트)
- [x] 추가 캡처 (시험 서버 8766, 같은 manual 데이터): 새 하네스 창 · 대상 회사 창 · 자료 만들기 창 · 폴더 지침 편집 · 새 용도 창 ·
      용도 아래쪽(프롬프트 구성 · 도구 · 파라미터) · 용도 미리보기 · 공통 블록 · 변수 · 실행 파라미터 · 변경 기록 · 지침 보기(CLI) · 새 워크플로 창
- [x] docs/사용자설명서.md 8장 다시 쓰기 (원리 · 파일 작성법 · 화면별 절차 · 처음 세팅 순서) — 개인정보 가리기 확인
- [x] 시험 서버 정리 · 진행표 마무리 · cron 지우기

---

# 이사: C:\workspace\Agent → C:\workspace\Agent-Tools (2026-11-14 요청: "옮기고 정상동작 확인, git 에 올릴 준비, 불필요한 것 지우기")

Agent-Tools = github.com/MizzleAa/Agent-Tools 빈 clone (main, 커밋 없음). 같은 C: 드라이브라 폴더째 옮기기(이름 바꾸기)는 즉시.
**이 파일도 옮긴 뒤에는 C:\workspace\Agent-Tools\.claude\work\progress.md 에서 이어서 한다.**

- [x] 조사: .gitignore · requirements · 절대 경로(C:\workspace\Agent) 쓰인 곳 · DB 안 경로 · learn/eval 내용
- [x] 진행 중 작업 확인 후 관리 서버 · 모델 서버 끄기
- [x] 옮기기 (models · workspace · runtime · data · 코드 전부 → Agent-Tools, .git 은 그대로)
- [x] .venv 다시 만들기 (pip freeze 79개 그대로) (또는 옮겨서 고치기) · web node_modules
- [x] 절대 경로 고치기 (DB workdir · CLAUDE.md · 문서 · 설정) + Claude 메모리 새 프로젝트 폴더로 복사
- [x] 정상 동작 확인: pytest 664 통과 · vitest 26 · tsc · build · 서버 켜기(작업 스케줄러, 03:13) · 화면(8766) · 모델 서버(health ok) · CLI 3종 감지
- [ ] 불필요한 것 지우기 (.playwright-mcp · __pycache__ · .pytest_cache · 잠금 파일 등)
- [ ] git: .gitignore 정리 · 비밀값/큰 파일 점검 · 커밋 (push 는 사용자 확인 뒤)
- [ ] 옛 폴더 C:\workspace\Agent 정리 (사용자 확인 뒤) · 보고
