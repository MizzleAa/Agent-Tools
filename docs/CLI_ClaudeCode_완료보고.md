# CLI · Claude Code 통합 완료 보고 (2026-11-12)

요청: Claude Code CLI 를 쓰는 GUI 세션 매니저를 **같은 대화 화면**에서 — 모델 선택의 외부(온라인) 아래 `CLI` 로,
연결은 **관리 › 외부 AI 모델** 안의 탭으로. (처음엔 별도 앱 `C:\workspace\ClaudeCodeGUI` 로 만들었고, 그 엔진을 이 프로젝트로 옮겼다. 별도 앱은 사용자 요청으로 2026-11-12 삭제.)

이 보고서는 개발 세션이 직접 바꾼 파일을 적는다 (QA 실험 기록과 구분).

## 바꾼 파일

새로 만든 것
- `agent/claude_code/` — 엔진: `store.py`(SQLite) · `settings.py` · `cli.py`(CLI 찾기·로그인) · `transcript.py`(기록 읽기·정규화)
  · `procs.py`(프로세스·고아·다른 창 감지) · `manager.py`(세션 상태·스트리밍·승인·중단·복구) · `hub.py`(이벤트) · `service.py` · `errors.py` · `attachments.py`
- `server/api/claude_code.py` — `/api/v1/claude-code/…` 22개 경로 (관리자만, 실시간은 SSE)
- `web/src/features/claudecode/` — `CcChatView.tsx`(대화 화면 CLI 모드) · `CcThread.tsx` · `ToolCard.tsx` · `PermissionPanel.tsx`
  · `FolderPicker.tsx` · `CcConnection.tsx`(관리 탭) · `api.ts` · `actions.ts` · `store.ts` · `chat.ts`(+test) · `items.ts` · `lib.ts` · `types.ts`
- `tests/test_claude_code.py` (8개) · `requirements-claude-code.txt` · 이 보고서

고친 것
- `server/api/__init__.py` (라우터 등록, 관리자만) · `server/app.py` (오류 머리글 처리, 종료 때 CLI 정리)
- `web/src/App.tsx` (`/cc/:ccId`) · `features/chat/ChatView.tsx` (CLI 모드 전환) · `features/chat/Composer.tsx` (모델 선택을 `ModelSelect` 로 분리 + CLI 묶음)
- `state/AppState.tsx` (`cc:` 모델 선택 유지·준비 상태·주기적 갱신) · `features/sidebar/Sidebar.tsx` (최근에 CLI 대화, 상태 표시)
- `features/admin/ProvidersTab.tsx` (탭: API 연결 | CLI) · `ui/Icon.tsx` (아이콘 추가) · `ui/Markdown.tsx` (코드 블록 색 구분 — 기존 `features/files/highlight` 사용, 에이전트 대화에도 적용)
- `styles/app.css` (CLI 화면 스타일 추가) · `requirements.txt` (주석) · `README.md` (사용법 절) · `web/openapi.json`·`schema.d.ts` (계약 재생성)

## 확인

- 테스트: 백엔드 644 통과 (1 건너뜀 — 기존), 화면 26 통과. 계약(OpenAPI) 일치.
- 화면 (임시 데이터·로그인 끔으로 같은 앱을 8766 에 띄워 Playwright 로): 관리 › 외부 AI 모델 › CLI 탭 연결 상태,
  대화 화면에서 `⌘ Claude Code · Haiku` 선택 → 작업 폴더 고르기 → 보내기 → 명령 실행 승인 → 결과·코드 블록(색) → 새로고침 후 기록 복원
  → 로컬 모델로 바꾸면 일반 새 대화로. 사이드바 최근에 `CLI` 대화 표시.
- 엔진의 예외 시나리오(CLI 강제 종료·서버 강제 종료 후 재개·동시 세션·경로 변경·네트워크 단절·긴 기록)는 옮기기 전 별도 앱에서
  실제 CLI 통합 시험 10개로 통과했다 (그 시험 파일은 별도 앱과 함께 지워짐 — 이 프로젝트에는 CLI 없이 도는 tests/test_claude_code.py 만 있다).
- 관리 서버(8765)는 16:25 에 새 코드로 재시작 (작업 스케줄러 1회 작업). 화면 파일은 그 뒤에도 고쳐 다시 빌드함 — 새로고침하면 반영.

## 후속 수정 (사용자 요청)

- 별도 앱 `C:\workspace\ClaudeCodeGUI` 삭제.
- **작업 폴더 자동**: CLI 새 대화에서 폴더 고르기 창이 뜨던 것을 없앴다. 보내면 에이전트의 새 작업과 같은 함수
  (`agent/session.create_session_dir` → `workspace\날짜_시각_요청 첫 문장`)로 폴더를 만들고 그 안에서 시작한다
  (`POST /api/v1/claude-code/conversations`, 로그인 모드면 그 사람의 작업 폴더 아래). 이미 있는 폴더를 고르는 것은 선택 사항으로 남겼다.
  시험: `tests/test_claude_code.py::test_new_conversation_makes_work_folder_like_agent` + 임시 workspace 로 화면 확인. 관리 서버 16:35 재시작.

- **작업 위치** (로컬 에이전트·CLI 공통): 입력창에서 '작업 위치'를 고르면 새 작업 폴더를 그 아래에 만든다.
  위치는 기준 폴더(환경변수 `AGENT_WORK_ROOT`, 없으면 서버 아래 workspace) 아래 **상대 경로**만 — 절대 경로·`..`·기준 밖은 400,
  일반 사용자는 403. 바꾼 파일: `agent/locations.py`(work_root·work_base·browse_work_dirs), `server/api/runs.py`(`base_dir`, 실행마다 쓰기 허용),
  `server/api/admin.py`(`GET /admin/fs/dirs` — 기준 아래 폴더 목록), `server/api/claude_code.py`(`base_dir`),
  화면 `features/chat/BaseDirSelect.tsx`·`FolderPicker.tsx`(공용), `Composer.tsx`·`ChatView.tsx`·`CcChatView.tsx`.
  시험: `tests/test_work_base.py`(5) · `tests/test_claude_code.py`.

- **추론 강도 · 지침** (2026-11-12 17:08 서버 재시작):
  - CLI 입력창에 `추론:`(→ `--effort`) · `용도` 선택 · `지침` 보기. 순서는 로컬 입력창과 같게 모델 → 추론 → 용도 → 위치 → 승인.
  - 지침: `agent/claude_code/instructions.py` 가 로컬·API 와 같은 `Agent.prompt_sections()` 로 조립 → `system_prompt.append`,
    하네스 역할 → `agents`(서브에이전트), 하네스 폴더는 `add_dirs` 로 읽을 수 있게. 뺀 조각: environment · intranet_status · lang_standard.
    실제 CLI 에 물어 용도 이름("coder")을 답하는 것으로 붙은 것을 확인.
  - DB `sessions` 에 `profile` · `effort` 칸 추가 (기존 DB 는 시작할 때 자동으로 칸을 더함). API `GET /claude-code/instructions`.
  - 관리 › 외부 AI 모델 탭 이름: `API` · `CLI`. CLI 탭에 'CLI 에 붙는 지침' 미리보기.
  - 시험: `test_cli_instructions_use_same_prompts_and_harness`, `test_session_profile_effort_and_cli_options` (CLI 시작 옵션에 지침·effort 가 들어가는지).

- **하네스 여러 개 · Codex · Gemini** (2026-11-12 19:28 서버 재시작):
  - 하네스: 공통 `harness/` + 회사·프로젝트 `harnesses/<이름>/` (지침 · 용도 · 자료). 실행에는 공통 + 그 사람 회사의 회사 하네스 + 고른 하네스.
    `agent/harness.py`(scope · active · find_role · '@이름/경로'), `profiles.py`(scopes), `config.Settings.harness`, `loop.py`, `tools/knowledge.py`,
    `workflow.py`(노드 harness + 점검), `server/api/harness.py`(`?scope=` · `/harness/scopes`), `catalog.py`(`GET /harnesses`), `runs.py`(`harness`),
    `settings/views.py`·`schemas.py`(bundle.harnesses). 화면: 설정 › 하네스 탭 `📁 공통 · 🏢/📂 … · ＋ 새 하네스` · 하네스 안 '＋ 새 용도' ·
    워크플로 노드 하네스 선택 · 입력창 하네스 선택(모델 · 추론 · 하네스 · 용도 · 위치 · 승인). '용도별' → '용도'.
  - CLI 엔진: `agent/claude_code/engines.py`(Codex app-server · Gemini ACP 드라이버 · 찾기 · npm 설치 · 브라우저 로그인), `gemini_login_url.mjs`,
    `manager.py`(엔진 세션 · 승인 · 기록), `store.py`(sessions.engine · engine_messages), `instructions.py`(엔진별 도구 안내 · `dev_md_excludes`),
    `server/api/claude_code.py`(`engine` · status.engines · `/engines/{engine}/login|install`). 화면: 모델 선택 CLI 묶음에 Codex · Gemini,
    `CcChatView`(엔진별 이름 · 승인 방식 · 추론), `EngineSection.tsx`(CLI 탭 설치 · 로그인), `ToolCard`(Codex 'Patch' diff).
  - 개발용 `CLAUDE.md` + `.claude/work/progress.md` (세션이 끊겨도 이어서) — CLI 대화에는 `claudeMdExcludes` 로 붙지 않게 (실제 CLI 로 확인).
  - 시험: `tests/test_harness_scopes.py`(6) · `tests/test_cli_engines.py`(4, 가짜 CLI `tests/fake_engine_cli.py`) · conftest 가 진짜 CLI 를 찾지 않게.
    전체 661 통과. 화면은 임시 서버(8766)에서 확인 — 하네스 만들기 · 용도 · 입력창 · CLI 탭(진짜 Codex 0.167 · Gemini 0.68 감지, 로그인 주소 받기까지).

## 남은 일 · 한계

- 실제 서버는 로그인 사용 중이라 관리자 계정으로 들어가 직접 확인이 필요하다 (이 세션은 비밀번호가 없어 임시 서버로 확인).
- 일반 사용자에게는 CLI 가 보이지 않는다 (서버 PC 의 Claude 계정·파일 권한으로 일하므로 관리자만).
- 서버가 강제로 꺼지면 그 순간의 응답은 잃는다 (기록에 저장된 데까지 남고 '이어서 진행'으로 재개).
- Codex 는 '로그인됨'으로 나오지만 토큰이 만료돼 실제 호출이 안 된다 → CLI 탭 Codex '로그인'으로 다시 로그인 필요. Gemini 도 첫 로그인 필요.
  둘 다 실제 모델로 끝까지 대화한 시험은 로그인 뒤에 해야 한다 (지금은 가짜 CLI 로 프로토콜 · 승인 · 기록 · 재개를 시험).
- 로그인은 이 서버 PC 의 브라우저에서 해야 한다 (로그인 응답이 127.0.0.1 로 돌아온다).
