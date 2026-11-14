"""터미널에서 에이전트 실행하기.

    .venv\\Scripts\\python cli.py run "workspace 폴더 구조를 요약해줘"
    .venv\\Scripts\\python cli.py run "..." --profile coder --show-thinking
    .venv\\Scripts\\python cli.py tools
    .venv\\Scripts\\python cli.py profiles
"""

from __future__ import annotations

import argparse
import json
import sys

from agent.config import load_settings
from agent.events import (ApprovalRequested, ApprovalResolved, Error, Event, Final, LLMCall, RunStarted,
                          ToolCall, ToolResult)
from agent.loop import Agent
from agent.profiles import list_profiles, load_profile
from agent.tools import REGISTRY

DIM, BOLD, CYAN, GREEN, RED, YELLOW, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def _indent(text: str, limit: int = 600) -> str:
    text = text if len(text) <= limit else text[:limit] + f" ... (+{len(text) - limit}자)"
    return "\n".join("     " + line for line in text.splitlines())


def cli_approver(req: ApprovalRequested) -> bool:
    print(f"\n{YELLOW}{BOLD}⚠ 승인 필요{RESET}{YELLOW} — {req.reason}{RESET}")
    for key, value in req.arguments.items():
        print(f"{YELLOW}   {key}:{RESET}\n{_indent(str(value), 2000)}")
    if not sys.stdin.isatty():
        print(f"{RED}   (대화형 터미널이 아니라 자동 거부){RESET}")
        return False
    return input(f"{YELLOW}   승인할까요? [y/N]: {RESET}").strip().lower() in ("y", "yes", "ㅛ")


def print_event(ev: Event, show_thinking: bool) -> None:
    if isinstance(ev, RunStarted):
        print(f"{BOLD}▶ 실행 {ev.run_id}{RESET}  프로필={ev.profile}  모델={ev.model}")
    elif isinstance(ev, LLMCall):
        print(f"\n{CYAN}[스텝 {ev.step}] 모델 응답{RESET} {DIM}({ev.prompt_tokens}+{ev.completion_tokens} tok, {ev.seconds:.1f}s){RESET}")
        if show_thinking and ev.reasoning:
            print(f"{DIM}{_indent(ev.reasoning, 1500)}{RESET}")
        if ev.tool_calls and ev.content:
            print(_indent(ev.content))
    elif isinstance(ev, ToolCall):
        print(f"  {BOLD}→ {ev.name}{RESET}({json.dumps(ev.arguments, ensure_ascii=False)[:300]})")
    elif isinstance(ev, ApprovalResolved):
        print(f"   {GREEN + '승인됨' if ev.approved else RED + '거부됨'}{RESET}")
    elif isinstance(ev, ToolResult):
        mark = f"{GREEN}←{RESET}" if ev.ok else f"{RED}← 실패{RESET}"
        print(f"  {mark} {DIM}{ev.seconds:.1f}s{RESET}\n{DIM}{_indent(ev.output, 400)}{RESET}")
    elif isinstance(ev, Error):
        print(f"{RED}✖ {ev.message}{RESET}")
    elif isinstance(ev, Final):
        color = GREEN if ev.status == "done" else YELLOW
        print(f"\n{color}{BOLD}■ {ev.status}{RESET}  스텝 {ev.steps} · 토큰 {ev.prompt_tokens}+{ev.completion_tokens} · {ev.seconds:.1f}s")
        print(f"\n{ev.answer}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="cli.py", description="로컬 Qwen 에이전트")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="작업 실행")
    run.add_argument("task")
    run.add_argument("--profile", default="general")
    run.add_argument("--model", help="settings.yaml 의 모델 대신 사용할 모델")
    run.add_argument("--max-steps", type=int)
    run.add_argument("--show-thinking", action="store_true", help="모델의 사고 과정도 출력")
    sub.add_parser("tools", help="등록된 도구 목록")
    sub.add_parser("profiles", help="프로필 목록")
    crawl_cmd = sub.add_parser("crawl", help="사내 사이트 수집 (settings.yaml 의 intranet.sources)")
    crawl_cmd.add_argument("source", nargs="?", default="", help="사이트 이름. 비우면 전부")
    args = parser.parse_args()

    if args.cmd == "crawl":
        from agent.crawler import crawl
        settings = load_settings()
        sources = [s for s in settings.intranet.sources if not args.source or s.name == args.source]
        if not sources:
            print(f"{RED}등록된 사이트가 없습니다 (config/settings.yaml 의 intranet.sources){RESET}")
            return 1
        for src in sources:
            print(f"{BOLD}▶ {src.name}{RESET} 수집 시작: {', '.join(src.start_urls)}")
            st = crawl(settings, src, on_page=lambda s: print(f"  {s.pages:>4}쪽  {DIM}{s.current_url}{RESET}"))
            color = GREEN if st.state == "done" else YELLOW
            print(f"{color}■ {st.state}{RESET}  색인 {st.pages}쪽 · 바뀜 {st.changed} · 삭제 {st.removed} · 오류 {st.errors}"
                  + (f"\n  최근 오류: {st.last_error}" if st.last_error else ""))
        return 0

    if args.cmd == "tools":
        for t in REGISTRY.values():
            print(f"{BOLD}{t.name}{RESET} [{t.permission}] — {t.description}")
        return 0
    if args.cmd == "profiles":
        for name in list_profiles():
            p = load_profile(name)
            print(f"{BOLD}{name}{RESET} — {p.description}\n   도구: {', '.join(p.tools)}")
        return 0

    settings = load_settings()
    if args.model:
        settings.backend.model = args.model
    profile = load_profile(args.profile)
    if args.max_steps:
        profile.max_steps = args.max_steps
    from agent.session import create_session_dir
    workdir = create_session_dir(settings.workspace_path, args.task) if settings.session_folders else None
    agent = Agent(settings, profile, approver=cli_approver, workdir=workdir)
    if workdir:
        print(f"{DIM}작업 폴더: {workdir}{RESET}")
    final = None
    try:
        for ev in agent.run(args.task):
            print_event(ev, args.show_thinking)
            if isinstance(ev, Final):
                final = ev
    except KeyboardInterrupt:
        agent.cancel.set()
        print(f"\n{YELLOW}중지됨{RESET}")
        return 130
    from agent.crawler import running_jobs, wait_jobs
    if running_jobs():   # 채팅으로 사이트를 등록해 수집이 시작됐다면, 끝날 때까지 기다린다 (창을 닫으면 끊김)
        print(f"\n{DIM}사내 사이트 수집이 끝날 때까지 기다립니다... (Ctrl+C 로 그만){RESET}")
        try:
            wait_jobs()
        except KeyboardInterrupt:
            pass
    return 0 if final and final.status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
