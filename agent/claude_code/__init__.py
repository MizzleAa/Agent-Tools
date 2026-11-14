"""Claude Code 세션 매니저 (관리 › Claude Code 탭).

이 PC 에 설치·로그인된 Claude Code CLI 를 Claude Agent SDK 로 구동해, 프로젝트·세션·대화·권한 승인을 화면에서 한다.
로컬 에이전트(agent/loop.py)와는 별개의 기능이다 — 대화는 Claude Code 가 처리하고, 여기서는 관리와 중계만 한다.

    store.py        SQLite (data/claude_code/claude_code.db): 프로젝트·세션 메타데이터·설정·프로세스 기록 — 대화 내용은 저장하지 않는다
    cli.py          설치된 claude.exe 찾기 · 버전 · 로그인 상태
    transcript.py   세션 기록(~/.claude/projects) 읽기 (SDK 공식 함수, 읽기 전용) · 메시지 정규화
    procs.py        CLI 프로세스 시작·종료 · 레지스트리 · 고아/다른 창 실행 감지
    manager.py      세션 상태 머신 · 스트리밍 · 권한 요청 중계 · 중단 · 재시작 복구
    hub.py          화면으로 보내는 이벤트 (SSE)
    service.py      프로젝트·세션 관리 + 한 개뿐인 서비스 객체(get_service)

원칙: 대화 기록의 원본은 Claude Code 세션 기록이고 고치지 않는다. 권한 승인 절차를 건너뛰는 모드는 없다.
claude-agent-sdk 는 선택 의존성이다 (requirements-claude-code.txt) — 없으면 이 탭만 '설치 필요'로 보인다.
"""
