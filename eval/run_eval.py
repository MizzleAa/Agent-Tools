"""eval: 정해진 과제를 돌려 에이전트를 점수로 비교한다. 프롬프트·모델·설정을 바꿨을 때
'좋아졌다/나빠졌다'를 느낌이 아니라 숫자로 판단하기 위한 도구다.

    .venv\\Scripts\\python eval\\run_eval.py
    .venv\\Scripts\\python eval\\run_eval.py --model qwen3.5:9b --repeat 3
    .venv\\Scripts\\python eval\\run_eval.py --only read_value python_sum

과제마다 data/eval/work/<id> 에 새 임시 폴더를 만들고, 에이전트는 그 폴더 안에서만 동작한다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import AllowedDir, load_settings  # noqa: E402
from agent.events import ApprovalRequested, Final  # noqa: E402
from agent.loop import Agent  # noqa: E402
from agent.profiles import load_profile  # noqa: E402
from agent.tools import REGISTRY  # noqa: E402

TASKS_DIR = Path(__file__).resolve().parent / "tasks"


class Checks(BaseModel):
    answer_contains: list[str] = Field(default_factory=list)  # 모두 포함
    answer_any: list[str] = Field(default_factory=list)       # 하나 이상 포함
    file_contains: dict[str, list[str]] = Field(default_factory=dict)
    tools_used: list[str] = Field(default_factory=list)
    max_steps: int | None = None


class Task(BaseModel):
    id: str
    task: str
    profile: str = "general"
    approve: Literal["none", "writes", "all"] = "none"
    files: dict[str, str] = Field(default_factory=dict)
    checks: Checks = Field(default_factory=Checks)


def approver_for(level: str):
    def approve(req: ApprovalRequested) -> bool:
        perm = REGISTRY[req.name].permission
        return level == "all" or (level == "writes" and perm == "write")
    return approve


def grade(task: Task, final: Final, work: Path) -> list[str]:
    c, answer, failures = task.checks, final.answer.lower(), []
    if final.status != "done":
        failures.append(f"status={final.status}")
    failures += [f"답변에 '{s}' 없음" for s in c.answer_contains if s.lower() not in answer]
    if c.answer_any and not any(s.lower() in answer for s in c.answer_any):
        failures.append(f"답변에 {c.answer_any} 중 아무것도 없음")
    for rel, needles in c.file_contains.items():
        f = work / rel
        if not f.exists():
            failures.append(f"파일 없음: {rel}")
            continue
        text = f.read_text(encoding="utf-8", errors="replace").lower()
        failures += [f"{rel} 에 '{s}' 없음" for s in needles if s.lower() not in text]
    failures += [f"도구 미사용: {t}" for t in c.tools_used if t not in final.tools_used]
    if c.max_steps and final.steps > c.max_steps:
        failures.append(f"스텝 초과 {final.steps}>{c.max_steps}")
    return failures


def run_task(task: Task, model: str | None) -> dict:
    settings = load_settings()
    if model:
        settings.backend.model = model
    work = settings.data_path / "eval" / "work" / task.id
    shutil.rmtree(work, ignore_errors=True)
    for rel, content in task.files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(content, encoding="utf-8")
    work.mkdir(parents=True, exist_ok=True)
    settings.workspace = str(work)
    settings.allowed_dirs = [AllowedDir(path=str(work), mode="rw")]

    agent = Agent(settings, load_profile(task.profile), approver=approver_for(task.approve))
    final = next(ev for ev in agent.run(task.task) if isinstance(ev, Final))
    failures = grade(task, final, work)
    return {"id": task.id, "passed": not failures, "failures": failures, "status": final.status,
            "steps": final.steps, "tokens": final.prompt_tokens + final.completion_tokens,
            "seconds": final.seconds, "tools": final.tools_used, "answer": final.answer, "run_id": agent.run_id}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--repeat", type=int, default=1, help="과제마다 반복 횟수 (소형 모델은 결과가 들쭉날쭉하다)")
    args = ap.parse_args()

    tasks = [Task.model_validate(yaml.safe_load(p.read_text(encoding="utf-8"))) for p in sorted(TASKS_DIR.glob("*.yaml"))]
    if args.only:
        tasks = [t for t in tasks if t.id in args.only]
    model = args.model or load_settings().backend.model

    results = []
    for task in tasks:
        for i in range(args.repeat):
            r = run_task(task, args.model)
            results.append(r)
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"{mark}  {task.id:<16} #{i + 1}  스텝 {r['steps']:>2}  토큰 {r['tokens']:>6}  {r['seconds']:>6.1f}s"
                  + ("" if r["passed"] else f"  ← {'; '.join(r['failures'])}"), flush=True)

    passed = sum(r["passed"] for r in results)
    total_s = sum(r["seconds"] for r in results)
    print(f"\n모델 {model}: {passed}/{len(results)} 통과, 총 {total_s:.0f}s")

    out = load_settings().data_path / "eval" / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}_{model.replace(':', '-')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": model, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"결과 저장: {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
