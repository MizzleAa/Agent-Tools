"""CLI(Claude Code · Codex · Gemini)에 붙이는 지침 — 로컬·API 모델과 같은 '설정 · 프롬프트'(용도·블록)와 하네스를 쓴다.

로컬·API 모델은 agent/loop.py 의 Agent.prompt_sections() 로 시스템 프롬프트를 조립한다. 여기서도 같은 함수로 조립하고,
Claude Code 에 그대로 맞지 않는 조각만 뺀다 / 바꾼다:
- 뺀다: environment(이 서버 에이전트의 샌드박스·셸·작업 폴더 — Claude Code 는 자기 환경 정보를 갖는다),
        intranet_status(사내 API 도구는 이 서버 에이전트에만 있다), lang_standard(실행 중 언어를 보고 붙는 조각)
- 바꾼다: knowledge(하네스 자료) — search_knowledge 도구 대신 자료 폴더를 Read/Grep 으로 읽으라고 알린다
- 하네스 역할(agents/*.md)은 Claude Code 서브에이전트로도 넘긴다 (Task 도구로 맡길 수 있게)
결과는 Claude Code 기본 시스템 프롬프트 뒤에 덧붙인다 (도구 사용법 등 Claude Code 자체 지침은 그대로).
Codex 는 developerInstructions 로, Gemini 는 대화 첫 요청 앞에 붙인다 (engines.py). 도구 이름 안내만 엔진마다 다르다 (TOOL_NOTES).
CLI 는 대화의 첫 요청 때 시스템 프롬프트를 기록해 재개 때도 그대로 쓰므로, 지침을 바꾸면 새 대화부터 적용된다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent import config, harness
from agent.loop import Agent
from agent.profiles import list_profiles, load_profile

SKIP_SECTIONS = {"environment", "intranet_status", "lang_standard"}
DEFAULT_PROFILE = "general"

_HEAD = "아래는 이 서버의 관리자가 '설정 · 프롬프트'와 하네스에 정해 둔 지침이다. 로컬·API 모델과 똑같이 따른다.\n"
_TAIL = " 이 서버에만 있는 도구(api_get, search_intranet 등)는 쓸 수 없으니 필요하면 사용자에게 알린다."
TOOL_NOTES = {
    "claude": _HEAD + "지침에 나오는 도구 이름은 이 서버 에이전트 기준이다 — 같은 일을 하는 Claude Code 도구로 바꿔 쓴다: "
              "read_file→Read · write_file→Write/Edit · list_dir/search_files→Glob/Grep · run_command/run_python→Bash(PowerShell) · "
              "fetch_url→WebFetch · web_search→WebSearch." + _TAIL,
    "codex": _HEAD + "지침에 나오는 도구 이름은 이 서버 에이전트 기준이다 — Codex 에서는 같은 일을 셸 명령(PowerShell)과 apply_patch 로 한다: "
             "read_file/list_dir/search_files→읽기 명령(Get-Content · Get-ChildItem · rg) · write_file→apply_patch · "
             "run_command/run_python→셸 명령 · fetch_url/web_search→웹 검색 도구(있으면)." + _TAIL,
    "gemini": _HEAD + "지침에 나오는 도구 이름은 이 서버 에이전트 기준이다 — 같은 일을 하는 Gemini CLI 도구로 바꿔 쓴다: "
              "read_file→read_file · write_file→write_file/replace · list_dir/search_files→list_directory/glob/search_file_content · "
              "run_command/run_python→run_shell_command(PowerShell) · fetch_url→web_fetch · web_search→google_web_search." + _TAIL,
}
TOOL_NOTE = TOOL_NOTES["claude"]


class _ModelName:   # prompt_preview 와 같은 방식 — 조립만 하고 모델은 부르지 않는다
    def __init__(self, model: str):
        self.model = model


def _profile_name(name: str | None, scopes: list[str] | None = None) -> str:
    names = list_profiles(scopes=scopes)
    if name and name in names:
        return name
    return DEFAULT_PROFILE if DEFAULT_PROFILE in names else (names[0] if names else DEFAULT_PROFILE)


def build(profile: str | None, workdir: Path | None = None, settings: config.Settings | None = None,
          harness_id: str = "", engine: str = "claude") -> dict[str, Any]:
    """{profile, harness, sections:[{id,title,text}], text, roles:[{name,description}], dirs} — text 가 Claude Code 에 덧붙는 글.
    harness_id: 고른 회사·프로젝트 하네스 (공통 + 그 하네스가 붙는다 — 로컬·API 와 같다)."""
    s = (settings or config.load_settings()).model_copy(deep=True)
    s.harness = harness_id if harness.usable(harness_id, s.audience) else ""
    scopes = harness.active(s.audience, s.harness)
    if workdir is not None and workdir.is_dir():
        # 조립만 하므로, 대화 폴더(작업 위치·가져온 세션이면 workspace 밖일 수 있다)를 이 조립의 작업 폴더로 둔다 — 그 폴더의 AGENT.md 등이 들어간다
        s.workspace = str(workdir)
        s.allowed_dirs = [config.AllowedDir(path=str(workdir), mode="rw")]
    else:
        workdir = None
    name = _profile_name(profile, scopes)
    agent = Agent(s, load_profile(name, scopes=scopes), llm=_ModelName("claude-code"), workdir=workdir, record=False)   # type: ignore[arg-type]
    sections: list[dict[str, str]] = []
    for bid, title, text in agent.prompt_sections():
        if bid in SKIP_SECTIONS or not text.strip():
            continue
        if bid == "knowledge":
            where = " · ".join(f"`{harness.root(sc)}`" for sc in scopes)
            prefixed = "".join(f"\n- `@{sc}/…` → `{harness.root(sc)}` 아래" for sc in scopes if sc)
            text = (f"관리자가 정리한 자료(사용설명서 · 에러 대처 · 점검 절차 등)가 {where} 아래에 있다. "
                    + ("장비 사용법·오류·절차를 물으면 기억으로 답하지 말고 그 폴더에서 Grep 으로 찾고 Read 로 읽은 뒤 답한다."
                       if engine == "claude" else "장비 사용법·오류·절차를 물으면 기억으로 답하지 말고 그 폴더에서 찾아 읽은 뒤 답한다.")
                    + (f"\n목록의 경로가 '@이름/' 으로 시작하면 다른 하네스 자료다:{prefixed}" if prefixed else "")
                    + "\n" + harness.catalog(s.audience, 150, scopes))
        sections.append({"id": bid, "title": title, "text": text.strip()})
    body = "\n\n".join(x["text"] if x["text"].lstrip().startswith("#") else f"## {x['title']}\n{x['text']}" for x in sections)
    label = f"용도: {name}" + (f" · 하네스: {t}" if (t := _title(s.harness)) else "")
    text = f"# 이 서버의 지침 ({label})\n{TOOL_NOTES.get(engine, TOOL_NOTE)}\n\n{body}" if sections else ""
    return {"profile": name, "harness": s.harness, "sections": sections, "text": text, "roles": roles(scopes),
            "dirs": [str(harness.root(sc)) for sc in scopes]}


def _title(sid: str) -> str:
    sc = harness.get_scope(sid) if sid else None
    return sc.title if sc else ""


def roles(scopes: list[str] | None = None) -> list[dict[str, str]]:
    """하네스 역할 → Claude Code 서브에이전트 정의 (도구는 Claude Code 기본을 그대로 물려받는다). 같은 이름은 고른 하네스 것."""
    out = []
    for name in harness.all_role_names(scopes):
        found = harness.find_role(name, scopes)
        if not found:
            continue
        meta, body = harness.split_front(found[1])
        if body.strip():
            out.append({"name": name, "description": str(meta.get("description") or name), "prompt": body.strip()})
    return out


def sdk_agents(scopes: list[str] | None = None) -> dict[str, Any] | None:
    from claude_agent_sdk import AgentDefinition

    rs = roles(scopes)
    return {r["name"]: AgentDefinition(description=r["description"], prompt=r["prompt"]) for r in rs} or None


def dev_md_excludes() -> list[str]:
    """이 프로그램 폴더(Agent)의 개발용 CLAUDE.md 는 CLI 대화에 붙이지 않는다 (claudeMdExcludes).
    작업 폴더(workspace)가 그 아래에 있어 Claude Code 가 부모 폴더의 CLAUDE.md 까지 읽기 때문 — 작업 폴더 자체의 지침 파일은
    build() 가 project_instructions 로 넣는다. 패턴은 /·\\ 두 모양과 끝 경로(**/…) 모양으로 (2.1.292 에서 확인)."""
    root = config.ROOT.resolve()
    out: list[str] = []
    for rel in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", ".claude/rules/**"):
        out += [f"{root.as_posix()}/{rel}", str(root / rel), f"**/{root.parent.name}/{root.name}/{rel}"]
    return out


def scopes_of(harness_id: str, settings: config.Settings | None = None) -> list[str]:
    s = settings or config.load_settings()
    return harness.active(s.audience, harness_id if harness.usable(harness_id, s.audience) else "")
