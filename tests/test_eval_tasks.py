"""eval 과제 파일 점검 (10단계 — 과제 15개 이상). 진짜 모델로 돌리는 것은 eval/run_eval.py (CI 에서는 하지 않는다):
여기서는 과제 파일이 바른지 — 읽히는지 · 이름이 겹치지 않는지 · 용도와 도구가 있는지 · 채점이 뜻대로인지 — 만 본다."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent.events import Final
from agent.profiles import load_profile
from agent.tools import REGISTRY
from eval.run_eval import TASKS_DIR, Task, grade

TASKS = [(p, Task.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))) for p in sorted(TASKS_DIR.glob("*.yaml"))]


def test_at_least_15_tasks_with_unique_ids_matching_file_names():
    assert len(TASKS) >= 15
    ids = [t.id for _, t in TASKS]
    assert len(set(ids)) == len(ids)
    assert all(p.stem == t.id for p, t in TASKS)


@pytest.mark.parametrize("path,task", TASKS, ids=[t.id for _, t in TASKS])
def test_task_profile_tools_and_approval_fit(path, task):
    profile = load_profile(task.profile)
    for tool in task.checks.tools_used:
        assert tool in REGISTRY, f"{task.id}: 없는 도구 {tool}"
        assert tool in profile.tools, f"{task.id}: 용도 {task.profile} 에 없는 도구 {tool}"
        perm = REGISTRY[tool].permission
        assert task.approve == "all" or perm == "read" or (task.approve == "writes" and perm == "write"), \
            f"{task.id}: {tool}({perm}) 를 쓰려면 approve 가 더 넓어야 한다"
    assert task.checks.answer_contains or task.checks.answer_any or task.checks.file_contains or task.checks.file_lacks, \
        f"{task.id}: 채점 기준이 없다"
    for rel in task.files:
        assert not Path(rel).is_absolute() and ".." not in Path(rel).parts, f"{task.id}: 과제 파일은 작업 폴더 안으로"


def test_grade_checks_answer_files_tools_and_steps(tmp_path):
    task = Task(id="t", task="x", checks={"answer_contains": ["800"], "answer_any": ["OK", "통과"], "tools_used": ["read_file"],
                                          "file_contains": {"a.py": ["len(xs)"]}, "file_lacks": {"a.py": ["+ 1"]}, "max_steps": 3})
    (tmp_path / "a.py").write_text("return sum(xs) / len(xs)\n", encoding="utf-8")
    good = Final(status="done", answer="800시간마다, 테스트 통과", steps=2, prompt_tokens=1, completion_tokens=1, seconds=1,
                 tools_used=["read_file"])
    assert grade(task, good, tmp_path) == []
    (tmp_path / "a.py").write_text("return sum(xs) / (len(xs) + 1)\n", encoding="utf-8")
    bad = Final(status="max_steps", answer="모름", steps=5, prompt_tokens=1, completion_tokens=1, seconds=1, tools_used=[])
    got = grade(task, bad, tmp_path)
    assert any("status" in f for f in got) and any("800" in f for f in got) and any("아직 '+ 1'" in f for f in got)
    assert any("도구 미사용" in f for f in got) and any("스텝 초과" in f for f in got)


def test_grade_ignores_thousands_separators(tmp_path):
    task = Task(id="t", task="x", checks={"answer_contains": ["320000", "106000"]})
    ok = Final(status="done", answer="서울 320,000원 · 대구 106,000원", steps=1, prompt_tokens=1, completion_tokens=1, seconds=1)
    assert grade(task, ok, tmp_path) == []
