"""워크플로: 노드(하네스 + 용도 + 지시문)를 정해진 순서로 실행하고, 앞 노드의 결과를 뒤 노드에 넘긴다.

    조사(researcher) ──▶ 정리(general) ──▶ 검토(coder)
         {{input}}          {{research}}       {{write}}

- 순서는 모델이 아니라 프로그램이 정한다. 소형 모델은 일을 스스로 나누고 합치는 걸 잘 못하므로, 사람이 나눠 둔
  순서대로 노드마다 한 가지 일만 시킨다.
- 노드마다 새 에이전트(새 컨텍스트)로 시작한다. 앞 노드의 긴 도구 결과가 뒤 노드의 컨텍스트를 차지하지 않는다.
- 노드는 하나씩 차례로 돈다 (GPU 하나 · 모델 서버 슬롯 하나). 같은 대화 폴더를 같이 쓰므로 앞 노드가 만든 파일을
  뒤 노드가 읽을 수 있다. 노드 결과 전문은 폴더의 _workflow/ 에도 남는다.
- after: 이 노드가 기다리는 앞 노드들. 그 중 하나라도 실패했으면 이 노드는 건너뛴다.
  on_fail: stop(기본, 실패하면 워크플로를 멈춤) | continue(이 노드에 기대지 않는 다른 노드는 계속).

config/workflows/<이름>.yaml 하나가 워크플로 하나다. 관리 화면 '설정 › 워크플로'에서 만들고 고친다.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent import harness, prompts, session
from agent.config import CONFIG_DIR, Settings
from agent.events import Event, Final, NodeFinished, NodeStarted, RunStarted, WorkflowStarted
from agent.loop import Agent, Approver, deny_all
from agent.profiles import load_profile

WORKFLOWS_DIR = CONFIG_DIR / "workflows"
DEFAULT_DIR = prompts.DEFAULTS_DIR / "workflows"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
NODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")    # {{노드id}} 로 쓰이므로 변수 이름 규칙과 같다
MAX_NODES = 12
OUTPUT_CHARS = 6000          # 앞 노드 결과를 뒤 노드 지시문에 넣을 때의 최대 글자 (넘으면 앞뒤만 + 파일 안내)
OK = ("done", "partial")     # 이 상태로 끝난 노드는 '성공' (뒤 노드가 이어 받는다)
RESERVED = {"input", "previous"} | set(prompts.BUILTIN_VARS)


class WorkflowNode(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)   # API 응답 문서에서는 기본값 필드도 '항상 옴'
    id: str
    title: str = ""
    harness: str = ""                 # 이 노드에 붙일 하네스 (harnesses/<이름>). '' = 실행에서 고른 하네스 (없으면 공통만)
    profile: str
    prompt: str
    after: list[str] | None = None    # None = 바로 앞 노드 (첫 노드는 없음)
    max_steps: int | None = Field(default=None, ge=0)
    on_fail: Literal["stop", "continue"] = "stop"

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not NODE_RE.match(v) or v in RESERVED:
            raise ValueError(f"노드 id '{v}': 영어 소문자로 시작하고 영어 소문자·숫자·_ 만 (예약어 input·previous·cwd 등 제외)")
        return v


class Workflow(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)   # API 응답 문서에서는 기본값 필드도 '항상 옴'
    name: str
    description: str = ""
    nodes: list[WorkflowNode]

    @field_validator("nodes")
    @classmethod
    def _nodes(cls, v: list[WorkflowNode]) -> list[WorkflowNode]:
        if not v:
            raise ValueError("노드가 하나 이상 있어야 합니다")
        if len(v) > MAX_NODES:
            raise ValueError(f"노드는 {MAX_NODES}개 이하로 두세요")
        return v

    @model_validator(mode="after")
    def _graph(self) -> "Workflow":
        seen: list[str] = []
        for node in self.nodes:
            if node.id in seen:
                raise ValueError(f"노드 id '{node.id}' 가 두 번 있습니다")
            for dep in self.deps(node):
                if dep not in seen:   # 앞에 있는 노드만 기다릴 수 있다 → 순환이 생기지 않는다
                    raise ValueError(f"'{node.id}' 노드가 기다리는 '{dep}' 는 그보다 앞에 있는 노드여야 합니다")
            seen.append(node.id)
        return self

    def deps(self, node: WorkflowNode) -> list[str]:
        if node.after is not None:
            return list(node.after)
        i = next(k for k, n in enumerate(self.nodes) if n is node)
        return [self.nodes[i - 1].id] if i else []

    def summary(self) -> list[dict[str, Any]]:
        return [{"id": n.id, "title": n.title or n.id, "harness": n.harness, "profile": n.profile, "after": self.deps(n)}
                for n in self.nodes]

    def to_yaml(self) -> str:
        data = self.model_dump()
        for n in data["nodes"]:
            for key in ("title", "harness", "after", "max_steps"):
                if n.get(key) in (None, ""):
                    n.pop(key, None)
            if n.get("on_fail") == "stop":
                n.pop("on_fail")
            if n.get("prompt") and not n["prompt"].endswith("\n"):
                n["prompt"] += "\n"
        if not data["description"]:
            data.pop("description")
        return prompts.dump_yaml(data)


def workflow_path(name: str, folder: Path | None = None) -> Path:
    if not NAME_RE.match(name):
        raise ValueError("워크플로 이름은 영어 소문자·숫자·-·_ 로 32자 이하")
    return (folder or WORKFLOWS_DIR) / f"{name}.yaml"


def load_workflow(name: str, folder: Path | None = None) -> Workflow:
    path = workflow_path(name, folder)
    if not path.exists():
        raise FileNotFoundError(f"워크플로가 없습니다: {name}")
    return Workflow.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def list_workflows(folder: Path | None = None) -> list[str]:
    folder = folder or WORKFLOWS_DIR
    return sorted(p.stem for p in folder.glob("*.yaml") if NAME_RE.match(p.stem)) if folder.is_dir() else []


def clip(text: str, limit: int = OUTPUT_CHARS, file: str = "") -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 2 // 3], text[-limit // 3:]
    where = f" 전문은 {file} 에 있다 (read_file 로 읽을 수 있다)." if file else ""
    return f"{head}\n\n…(가운데 {len(text) - len(head) - len(tail):,}자 생략.{where})…\n\n{tail}"


class WorkflowRunner:
    """서버에서는 Agent 와 같은 모양으로 쓴다: run_id, cancel, run(task, history, parent_run_id)."""

    def __init__(self, settings: Settings, workflow: Workflow, llm: Any = None, approver: Approver = deny_all,
                 cancel: threading.Event | None = None, run_id: str | None = None, max_steps: int | None = None,
                 max_seconds: int | None = None, workdir: Path | None = None, previous: str = "",
                 resume: dict[str, str] | None = None, resume_note: str = "", input_text: str = ""):
        """previous: 같은 대화의 이전 실행 최종 답 ({{previous}}).
        resume: 이어서 하기 — 이전 실행에서 성공한 노드의 결과 {노드 id: 답}. 이 노드들은 다시 돌리지 않는다.
        resume_note: 이어서 하기일 때 처음 다시 도는 노드에게 알려 줄 이전 시도의 결과.
        input_text: {{input}} 값. 비우면 run() 의 task (이어서 하기에서는 처음 요청을 그대로 쓴다)."""
        for node in workflow.nodes:
            if node.harness and not harness.usable(node.harness, settings.audience if settings is not None else None):
                raise FileNotFoundError(f"'{node.title or node.id}' 노드의 하네스를 쓸 수 없습니다: {node.harness}")
            load_profile(node.profile, scopes=self._scopes(settings, node))   # 없는 프로필이면 시작 전에 FileNotFoundError
        self.settings = settings
        self.workflow = workflow
        self.llm = llm
        self.approver = approver
        self.cancel = cancel or threading.Event()
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self.workdir = workdir
        self.previous = previous
        self.resume = resume or {}
        self.resume_note = resume_note
        self.input_text = input_text

    @staticmethod
    def _scopes(settings: Settings | None, node: WorkflowNode) -> list[str]:
        audience, chosen = (settings.audience, settings.harness) if settings is not None else (None, "")
        return harness.active(audience, node.harness or chosen)

    @property
    def model(self) -> str:
        return getattr(self.llm, "model", None) or self.settings.backend.model

    def run(self, task: str, history: list[dict[str, Any]] | None = None, parent_run_id: str = "") -> Iterator[Event]:
        events: list[dict[str, Any]] = []
        for ev in self._run(task, parent_run_id):
            events.append(ev.to_dict())
            yield ev
        if self.workdir is not None:
            try:
                session.record_run(self.settings.workspace_path, self.workdir, events)
            except OSError as e:
                print(f"작업기록을 남기지 못했습니다: {e}")

    def _run(self, task: str, parent_run_id: str) -> Iterator[Event]:
        wf = self.workflow
        started = time.monotonic()
        yield RunStarted(self.run_id, task, f"workflow:{wf.name}", self.model, parent_run_id,
                         str(self.workdir) if self.workdir else "")
        base = {**prompts.load_prompts().values(), "input": self.input_text or task, "previous": self.previous,
                "today": date.today().isoformat()}
        yield WorkflowStarted(wf.name, wf.summary(), base["input"])
        outputs: dict[str, str] = {}
        status_of: dict[str, str] = {}
        totals = {"steps": 0, "prompt": 0, "completion": 0}
        used: list[str] = []
        stopped: tuple[str, str] | None = None   # (노드 제목, 상태)
        last_answer = ""
        note_pending = bool(self.resume_note)
        for i, node in enumerate(wf.nodes, 1):
            title = node.title or node.id
            if self.cancel.is_set():
                stopped = (title, "cancelled")
                break
            if node.id in self.resume:   # 이어서 하기: 지난번에 성공한 노드는 결과만 다시 쓴다
                outputs[node.id], status_of[node.id] = self.resume[node.id], "done"
                last_answer = outputs[node.id]
                yield NodeFinished(node.id, title, "reused", outputs[node.id])
                continue
            failed_deps = [d for d in wf.deps(node) if status_of.get(d) not in OK]
            if failed_deps:
                status_of[node.id] = "skipped"
                yield NodeFinished(node.id, title, "skipped", f"앞 노드({', '.join(failed_deps)})가 끝나지 않아 건너뜀")
                continue
            values = {**base, **{k: clip(v, file=self._output_file(k)) for k, v in outputs.items()}}
            prompt = prompts.render(node.prompt, values).strip()
            if note_pending:   # 이어서 하기로 다시 도는 첫 노드: 지난 시도가 어디서 멈췄는지 알려 준다
                prompt += ("\n\n(이전 시도가 끝나지 않고 멈췄습니다. 작업 폴더에 남은 파일을 먼저 확인하고 이어서 하세요. "
                           f"이전 시도의 마지막 보고:)\n{clip(self.resume_note, 2000)}")
                note_pending = False
            profile = load_profile(node.profile, scopes=self._scopes(self.settings, node))
            node_settings = self.settings
            if node.harness:   # 노드에서 고른 하네스 — 이 노드에만
                node_settings = self.settings.model_copy(update={"harness": node.harness})
            agent = Agent(node_settings, profile, llm=self.llm, approver=self.approver, cancel=self.cancel,
                          run_id=f"{self.run_id}-{node.id}", max_seconds=self.max_seconds, workdir=self.workdir,
                          max_steps=node.max_steps if node.max_steps is not None else self.max_steps, record=False)
            agent.ctx.run_dir = self.settings.data_path / "runs" / self.run_id / node.id   # 실행을 지우면 같이 지워지게
            yield NodeStarted(node.id, title, node.profile, i, len(wf.nodes), prompt)
            final: Final | None = None
            for ev in agent.run(prompt):
                if isinstance(ev, RunStarted):
                    continue
                if isinstance(ev, Final):
                    final = ev
                    continue
                yield ev
            final = final or Final("error", "(노드가 결과 없이 끝남)", 0, 0, 0, 0.0)
            outputs[node.id], status_of[node.id] = final.answer, final.status
            totals["steps"] += final.steps
            totals["prompt"] += final.prompt_tokens
            totals["completion"] += final.completion_tokens
            used += final.tools_used
            self._save_output(i, node.id, title, final)
            yield NodeFinished(node.id, title, final.status, final.answer, final.steps, final.prompt_tokens,
                               final.completion_tokens, final.seconds)
            if final.status in OK:
                last_answer = final.answer
            elif node.on_fail == "stop" or final.status in ("cancelled", "error"):
                stopped = (title, final.status)
                last_answer = final.answer
                break
        if stopped:
            status, answer = stopped[1], f"**워크플로가 '{stopped[0]}' 노드에서 멈췄습니다.**\n\n{last_answer}".rstrip()
        else:
            failures = [n.id for n in wf.nodes if status_of.get(n.id) not in OK]
            status = "partial" if failures or any(s == "partial" for s in status_of.values()) else "done"
            answer = last_answer + (f"\n\n(끝나지 못한 노드: {', '.join(failures)})" if failures else "")
        yield Final(status, answer, totals["steps"], totals["prompt"], totals["completion"],
                    round(time.monotonic() - started, 2), used)

    def _output_file(self, node_id: str) -> str:
        if self.workdir is None:
            return ""
        hit = next(iter(sorted((self.workdir / "_workflow").glob(f"*_{node_id}.md"))), None) \
            if (self.workdir / "_workflow").is_dir() else None
        return f"_workflow/{hit.name}" if hit else ""

    def _save_output(self, index: int, node_id: str, title: str, final: Final) -> None:
        if self.workdir is None:
            return
        try:
            folder = self.workdir / "_workflow"
            folder.mkdir(exist_ok=True)
            (folder / f"{index:02d}_{node_id}.md").write_text(
                f"# {index}. {title} ({final.status})\n\n{final.answer}\n", encoding="utf-8")
        except OSError:
            pass


def lint(workflow: Workflow) -> list[str]:
    """저장할 때 보여 줄 경고. 저장은 된다 (오류는 Workflow 검증이 막는다)."""
    out: list[str] = []
    known = {"input", "previous", "today"} | set(prompts.load_prompts().values())
    ids = [n.id for n in workflow.nodes]
    for i, node in enumerate(workflow.nodes):
        title = node.title or node.id
        if node.harness and harness.get_scope(node.harness) is None:
            out.append(f"'{title}': 하네스 '{node.harness}' 가 없습니다 (지워졌거나 이름이 바뀜) — 실행하면 멈춥니다")
        else:
            try:
                load_profile(node.profile, scopes=harness.active(None, node.harness))
            except (FileNotFoundError, ValueError):
                out.append(f"'{title}': 용도 '{node.profile}' 가 " + (f"하네스 '{node.harness}' 와 공통에 " if node.harness else "")
                           + "없습니다 — 실행하면 멈춥니다")
        refs = {v.strip() for v in prompts._ANY_VAR.findall(node.prompt)}
        later = sorted(r for r in refs if r in ids[i:])
        if later:
            out.append(f"'{title}': {', '.join('{{' + r + '}}' for r in later)} 는 이 노드보다 뒤(또는 자기 자신)라 아직 결과가 없습니다")
        unknown = sorted(refs - known - set(ids))
        if unknown:
            out.append(f"'{title}': 정의되지 않은 변수 {', '.join('{{' + r + '}}' for r in unknown)}")
        loose = sorted(r for r in refs if r in ids[:i] and r not in workflow.deps(node))
        if loose:
            out.append(f"'{title}': {', '.join(loose)} 의 결과를 쓰는데 '기다릴 노드'에 없습니다 — 그 노드가 실패해도 이 노드가 돕니다")
        if i and not refs & (set(ids[:i]) | {"input"}):
            out.append(f"'{title}': 앞 노드 결과({{{{노드id}}}})도 {{{{input}}}} 도 쓰지 않아 무엇을 할지 모릅니다")
    return out


def resume_state(events: list[dict[str, Any]], workflow: Workflow) -> tuple[dict[str, str], str, str]:
    """이전 워크플로 실행의 이벤트 → (성공한 노드 결과, 원래 입력, 멈춘 노드의 마지막 보고)."""
    start = next((e for e in events if e.get("type") == "WorkflowStarted"), {})
    done: dict[str, str] = {}
    note = ""
    ids = {n.id for n in workflow.nodes}
    for e in events:
        if e.get("type") != "NodeFinished" or e.get("node") not in ids:
            continue
        if e["status"] in ("done", "partial", "reused"):
            done[e["node"]] = e["answer"]
        elif e["status"] != "skipped" and not note:
            note = e["answer"]
    return done, start.get("input", ""), note
