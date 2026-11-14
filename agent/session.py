"""대화(세션)별 작업 폴더와 마크다운 작업 기록.

    workspace/
      작업목록.md                          ← 모든 세션 표 (자동)
      2026-11-05_1430_사진 뷰어 만들어줘/   ← 대화 하나 = 폴더 하나. 에이전트가 만든 파일은 여기에
        작업기록.md                         ← 실행마다 '요청 → 한 일 → 답변 → 폴더 안 파일' 을 덧붙인다 (자동)
        viewer.py

기록은 모델이 아니라 프로그램이 이벤트를 보고 정해진 형식으로 쓴다 (빠뜨리거나 지어내지 않게).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_NAME = "작업기록.md"
INDEX_NAME = "작업목록.md"
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')   # Windows 폴더 이름에 쓸 수 없는 글자
STATUS_KO = {"done": "완료", "partial": "부분 완료", "failed": "실패", "no_action": "실행 안 함(예고만 함)",
             "max_steps": "스텝 한도", "timeout": "시간 초과", "loop": "반복 감지로 멈춤", "cancelled": "중지됨", "error": "오류",
             "skipped": "건너뜀", "reused": "이전 결과 다시 씀"}
TOOL_KO = {
    "list_dir": "폴더 보기", "read_file": "파일 읽기", "search_files": "파일 찾기", "write_file": "파일 저장",
    "run_command": "명령 실행", "run_python": "파이썬 실행", "web_search": "웹 검색", "fetch_url": "페이지 읽기",
    "search_intranet": "사내 검색", "api_get": "API 조회", "api_send": "API 전송", "find_endpoint": "API 경로 찾기",
    "add_intranet_site": "사이트 등록", "crawl_site": "다시 수집", "add_api": "API 등록", "allow_host": "내부 주소 허용",
    "read_standard": "규격 읽기",
}


def folder_name(task: str, when: datetime | None = None) -> str:
    """'날짜_시각_요청 첫 문장'. 모델이 경로를 옮겨 적다 틀리지 않게(QA E1) 첫 문장까지만, 문장 부호 없이,
    단어 중간에서 자르지 않는다 ('만들어줘. (다른 대화에서 … 영' → '만들어줘')."""
    first = task.strip().splitlines()[0] if task.strip() else "작업"
    first = re.split(r"[.!?。]\s|[(\[{（]", first + " ")[0]
    words = re.sub(r"[^\w\s-]", " ", _INVALID.sub(" ", first)).split()
    title = ""
    for w in words:
        if len(title) + len(w) + 1 > 40:
            break
        title = f"{title} {w}".strip()
    title = title or (words[0][:40] if words else "작업")
    return f"{(when or datetime.now()):%Y-%m-%d_%H%M}_{title}"


def create_session_dir(workspace: Path, task: str) -> Path:
    base = workspace / folder_name(task)
    path, i = base, 2
    while path.exists():
        path, i = Path(f"{base}-{i}"), i + 1
    path.mkdir(parents=True)
    return path


def _summary(name: str, args: dict[str, Any]) -> str:
    if name in ("api_get", "api_send"):
        return f"{args.get('method', 'GET')} {args.get('api', '')}{args.get('path', '')}"
    for key in ("command", "path", "url", "host", "query", "name", "code"):
        if isinstance(args.get(key), str) and args[key]:
            return args[key].strip().splitlines()[0][:120]
    return ""


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.strip().splitlines())


def _files(folder: Path, limit: int = 60) -> list[str]:
    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.name != LOG_NAME and "__pycache__" not in p.parts:
            size = p.stat().st_size
            human = f"{size} B" if size < 1024 else f"{size / 1024:.1f} KB" if size < 2**20 else f"{size / 2**20:.1f} MB"
            out.append(f"- `{p.relative_to(folder).as_posix()}` ({human})")
            if len(out) >= limit:
                out.append("- … (더 있음)")
                break
    return out or ["- (아직 없음)"]


def record_run(workspace: Path, folder: Path, events: list[dict[str, Any]]) -> None:
    """실행 하나를 folder/작업기록.md 에 덧붙이고 workspace/작업목록.md 를 다시 만든다."""
    start = next((e for e in events if e["type"] == "RunStarted"), None)
    final = next((e for e in reversed(events) if e["type"] == "Final"), None)
    if start is None or final is None:
        return
    log = folder / LOG_NAME
    now = datetime.now()
    text = log.read_text(encoding="utf-8") if log.exists() else (
        f"# {start['task'].strip().splitlines()[0][:80]}\n\n"
        f"- 폴더: `{folder}`\n- 시작: {now:%Y-%m-%d %H:%M}\n"
        f"- 이 파일은 실행이 끝날 때마다 자동으로 정리됩니다.\n\n---\n")
    number = len(re.findall(r"^## \d+\.", text, flags=re.M)) + 1

    done: list[str] = []
    calls = {e["call_id"]: e for e in events if e["type"] == "ToolCall"}
    denied = {e["call_id"] for e in events if e["type"] == "ApprovalResolved" and not e["approved"]}
    for e in events:
        if e["type"] == "NodeStarted":   # 워크플로: 노드마다 한 줄 머리
            done.append(f"- ▶ 노드 {e['index']}/{e['total']} {e['title']} ({e['profile']})")
            continue
        if e["type"] == "NodeFinished":
            done.append(f"  - {e['title']} → {STATUS_KO.get(e['status'], e['status'])}")
            continue
        if e["type"] == "Note" and e.get("kind") in ("act", "gate", "failures", "evidence", "danger", "unfinished", "outside",
                                                     "regression", "fake_file", "credentials", "v1", "handoff", "language",
                                                     "directive", "pinned", "read_only", "steps", "grounding", "share"):
            done.append(f"- ↺ {e['message']}")
            continue
        if e["type"] != "ToolResult":
            continue
        call = calls.get(e["call_id"], {})
        args = call.get("arguments", {})
        what = f"{TOOL_KO.get(e['name'], e['name'])}: `{_summary(e['name'], args)}`"
        if e["call_id"] in denied:
            what += " — 거부함"
        elif not e["ok"]:
            what += " — 실패: " + e["output"].strip().splitlines()[0][:120] if e["output"].strip() else " — 실패"
        elif e["name"] in ("run_command", "run_python"):
            code = re.search(r"exit_code: (-?\d+)", e["output"])
            what += f" (종료 코드 {code.group(1)})" if code else ""
        done.append(f"- {what}")

    status = STATUS_KO.get(final["status"], final["status"])
    section = [
        f"## {number}. {start['task'].strip().splitlines()[0][:60]}",
        f"- {now:%Y-%m-%d %H:%M} · 프로필 {start['profile']} · 모델 {start['model']} · "
        f"결과: {status} · 스텝 {final['steps']} · {final['seconds']:.0f}초",
        "", "**요청**", "", _quote(start["task"]), "",
        "**한 일**", *(done or ["- (도구를 쓰지 않음)"]), "",
        "**답변**", "", (final["answer"] or "(빈 답변)").strip(), "",
        "**폴더 안 파일** (이 시점)", *_files(folder), "", "---", "",
    ]
    log.write_text(text.rstrip() + "\n\n" + "\n".join(section), encoding="utf-8")
    write_index(workspace)


def write_index(workspace: Path) -> None:
    """workspace/작업목록.md: 세션 폴더를 최근 순으로 표로 정리한다."""
    rows = []
    for folder in sorted((p for p in workspace.iterdir() if p.is_dir() and (p / LOG_NAME).exists()), reverse=True):
        log = (folder / LOG_NAME).read_text(encoding="utf-8")
        title = log.splitlines()[0].lstrip("# ").strip() if log else folder.name
        statuses = re.findall(r"결과: ([^·\n]+?) ·", log)
        turns = len(re.findall(r"^## \d+\.", log, flags=re.M))
        when = folder.name[:16].replace("_", " ", 1) if re.match(r"\d{4}-\d{2}-\d{2}_\d{4}", folder.name) else ""
        link = f"[{folder.name}](<{folder.name}/{LOG_NAME}>)"
        rows.append(f"| {when[:10]} {when[11:13]}:{when[13:15]} | {title.replace('|', '/')} | {turns} | "
                    f"{statuses[-1].strip() if statuses else '-'} | {link} |")
    body = ["# 작업 목록", "", "대화(세션)마다 폴더가 하나씩 있고, 각 폴더의 작업기록.md 에 자세한 내용이 있습니다. (자동 생성)", "",
            "| 시작 | 작업 | 횟수 | 마지막 결과 | 폴더 |", "|---|---|---|---|---|", *rows, ""]
    (workspace / INDEX_NAME).write_text("\n".join(body), encoding="utf-8")
