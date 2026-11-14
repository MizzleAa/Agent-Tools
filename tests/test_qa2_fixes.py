"""QA 2차(R06~R10) 결함 D1~D11 의 완료 기준을 가짜 모델로 확인한다.
특정 과제(동영상 플레이어)와 무관한 일반 시나리오만 쓰고, 언어별로 같은 규칙이 통하는지도 본다."""

import json
import shutil
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest

from agent import evidence
from agent.config import ROOT
from agent.context import ARG_NOTE
from agent.events import ApprovalRequested, Note, NodeFinished, ToolResult
from agent.llm import _parse_tool_call, leaked_call_syntax
from agent.loop import Agent
from agent.profiles import load_profile
from agent.safety import destructive_reason
from agent.workflow import Workflow, WorkflowRunner
from server.deps import RunHandle
from tests.conftest import FakeLLM, call, reply

TABLE = "| # | 요구 | 구현 위치 | 등급 | 근거 |\n|---|---|---|---|---|\n"


def run(agent, task="합계 계산 프로그램을 만들어줘"):
    return list(agent.run(task))


def results(events):
    return [e for e in events if isinstance(e, ToolResult)]


def coder(auto_writes=True):
    p = load_profile("coder")
    p.auto_approve_writes = auto_writes
    return p


def report(result, *rows, extra=""):
    return f"## 결과: {result}\n### 스택\nPython 3.12\n### 요구사항별 검증\n" + TABLE + "".join(rows) + extra


FAKE = report("완료", "| 1 | 합계 계산 | calc/core.py | V3 | `python -m pytest` 3 passed |\n",
              extra="\n### 파일 구조\n```\ncalc/\n├── core.py\n└── tests/test_core.py\n```\n")

PROJECT = {"calc/README.md": "# calc\n", "calc/requirements.txt": "", "calc/.gitignore": ".venv/\n",
           "calc/core.py": "def add(a, b):\n    return a + b\n"}
REAL_TEST = ("import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
             "from core import add\nassert add(1, 2) == 3\nprint('ok 1')\n")
LIB_TEST = "import json\nassert json.loads('[1, 2]') == [1, 2]\nprint('ok 1')\n"   # 프로젝트 코드를 거치지 않음


def write_all(files):
    return [reply(call("write_file", path=p, content=c)) for p, c in files.items()]


# ───────────────────────── D1. 도구 0회 '가짜 완료 보고'

def test_D1_zero_tool_report_is_sent_back_once_then_no_action(settings, ws):
    llm = FakeLLM([reply(content=FAKE), reply(content=FAKE)])
    events = run(Agent(settings, coder(), llm=llm, approver=lambda r: True))
    notes = [e for e in events if isinstance(e, Note)]
    assert [n.kind for n in notes] == ["evidence"]
    assert "도구를 한 번도" in llm.calls[1]["messages"][-1]["content"]
    final = events[-1]
    assert final.status == "no_action" and "실행 기록 점검" in final.answer


def test_D1_zero_tool_claim_without_report_form_in_general_profile(settings, ws):
    claim = "app.py 파일을 만들었고 실행해서 결과를 확인했습니다."
    events = run(Agent(settings, load_profile("general"), llm=FakeLLM([reply(content=claim)] * 2)))
    assert events[-1].status == "no_action"


def test_D1_plain_answers_without_tools_stay_done(settings, ws):
    for answer in ("파이썬의 리스트는 순서가 있는 자료형입니다.", "합계는 a + b 로 구하면 됩니다. 예: `add(1, 2)`"):
        events = run(Agent(settings, load_profile("general"), llm=FakeLLM([reply(content=answer)])), task="질문")
        assert events[-1].status == "done" and events[-1].answer == answer


def test_D1_D3_workflow_fix_node_with_zero_tools_is_not_done(settings, ws):
    """R10 흐름: 검토가 '심각' 문제를 냈는데 고치기 노드가 도구 없이 '완료' 보고 → 워크플로도 done 이 아니다."""
    w = Workflow.model_validate({"name": "t", "nodes": [
        {"id": "build", "profile": "general", "prompt": "{{input}}"},
        {"id": "review", "profile": "reviewer", "prompt": "검토: {{build}}"},
        {"id": "fix", "profile": "coder", "prompt": "고치기: {{review}}"}]})
    script = [reply(content="설계 초안: 합계 함수 하나"), reply(content="심각: 입력 검사가 없음"),
              reply(content=FAKE), reply(content=FAKE)]
    events = list(WorkflowRunner(settings, w, llm=FakeLLM(script), approver=lambda r: True, workdir=ws).run("합계"))
    done = {e.node: e.status for e in events if isinstance(e, NodeFinished)}
    assert done["fix"] == "no_action" and events[-1].status != "done"


# ───────────────────────── D2. 프로젝트 코드를 거치지 않은 근거

def _project_run(settings, test_name, test_code, answer, times=3):
    files = dict(PROJECT, **{f"calc/tests/{test_name}": test_code})
    script = write_all(files) + [reply(call("run_command", command=f"python calc/tests/{test_name}"))]
    script += [reply(content=answer)] * times
    agent = Agent(settings, coder(), llm=FakeLLM(script), approver=lambda r: True, max_steps=0)
    return run(agent)


def test_D2_test_that_only_exercises_a_library_is_not_done(settings, ws):
    answer = report("완료", "| 1 | 합계 계산 | calc/core.py | V3 | `python calc/tests/test_lib.py` → ok 1 |\n")
    events = _project_run(settings, "test_lib.py", LIB_TEST, answer)
    assert results(events)[-1].output.startswith("exit_code: 0")
    gates = [e for e in events if isinstance(e, Note) and e.kind == "gate"]
    assert gates and "프로젝트 자신의 코드" in gates[0].injected
    final = events[-1]
    assert final.status == "partial" and "프로젝트 자신의 코드" in final.answer


def test_D2_test_that_imports_project_code_is_done(settings, ws):
    answer = report("완료", "| 1 | 합계 계산 | calc/core.py | V3 | `python calc/tests/test_core.py` → ok 1 |\n")
    events = _project_run(settings, "test_core.py", REAL_TEST, answer, times=1)
    assert not [e for e in events if isinstance(e, Note) and e.kind == "gate"]
    assert events[-1].status == "done" and events[-1].answer == answer


def test_D2_claimed_run_that_never_happened_or_failed(tmp_path):
    (tmp_path / "core.py").write_text("x = 1\n", encoding="utf-8")
    answer = report("완료", "| 1 | 계산 | core.py | V2 | pytest 2 passed |\n")
    rv = evidence.review(answer, "t", [], tmp_path, used_any_tool=True, dev_report=True)
    assert any("돌린 기록이 없습니다" in f.text for f in rv.blocking)
    failed = [evidence.Step("run_command", {"command": "python -m pytest"}, True, "exit_code: 1\nFAILED")]
    rv = evidence.review(answer, "t", failed, tmp_path, used_any_tool=True, dev_report=True)
    assert any("실패했습니다" in f.text for f in rv.blocking)


def test_D2_missing_implementation_file_blocks(tmp_path):
    answer = report("부분 완료", "| 1 | 저장 | app/storage.py | V1 | 문법 검사 |\n")
    rv = evidence.review(answer, "t", [], tmp_path, used_any_tool=True, dev_report=True)
    assert any("app/storage.py" in f.text and "없습니다" in f.text for f in rv.blocking)


@pytest.mark.parametrize("files,test,expected", [
    # JavaScript: 상대 경로로 프로젝트 모듈을 불러오는가
    ({"web/src/sum.js": "module.exports = (a, b) => a + b;\n"}, ("web/test/sum.test.js", "const sum = require('../src/sum');\n"), True),
    ({"web/src/sum.js": "module.exports = 1;\n"}, ("web/test/lib.test.js", "const assert = require('node:assert');\n"), False),
    # C#: 테스트 프로젝트가 본 프로젝트를 참조하는가
    ({"cs/src/App/App.csproj": "<Project/>",
      "cs/tests/App.Tests/App.Tests.csproj": '<Project><ItemGroup><ProjectReference Include="..\\..\\src\\App\\App.csproj" /></ItemGroup></Project>'},
     ("cs/tests/App.Tests/SumTests.cs", "public class SumTests {}\n"), True),
    ({"cs/src/App/App.csproj": "<Project/>", "cs/tests/App.Tests/App.Tests.csproj": "<Project/>"},
     ("cs/tests/App.Tests/SumTests.cs", "public class SumTests {}\n"), False),
])
def test_D2_project_code_rule_is_language_neutral(tmp_path, files, test, expected):
    for p, c in {**files, test[0]: test[1]}.items():
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text(c, encoding="utf-8")
    assert evidence.exercises_project(tmp_path / test[0], tmp_path) is expected


# ───────────────────────── D4. 다른 동작으로 대체한 요구

def test_D4_substitution_written_in_row_or_interpretation_blocks(tmp_path):
    (tmp_path / "export.py").write_text("def export():\n    pass\n", encoding="utf-8")
    ok = [evidence.Step("run_command", {"command": "python export.py"}, True, "exit_code: 0\nok")]
    row = report("부분 완료", "| 1 | CSV 내보내기 | export.py | V3 | JSON 으로 대체, `python export.py` ok |\n")
    rv = evidence.review(row, "t", ok, tmp_path, used_any_tool=True, dev_report=True)
    assert any("V0" in f.text and "대체" in f.text for f in rv.blocking)

    interp = report("부분 완료", "| 1 | CSV 내보내기 | export.py | V3 | `python export.py` ok |\n",
                    extra="\n### 해석·한계\n- 내보내기는 CSV가 아닌 JSON으로 구현했습니다.\n")
    rv = evidence.review(interp, "t", ok, tmp_path, used_any_tool=True, dev_report=True)
    assert any("#1 CSV 내보내기" in f.text and "미충족" in f.text for f in rv.blocking)


@pytest.mark.parametrize("name,line", [
    ("store.py", "# TODO: 실제 저장 대신 메모리에만 보관"),
    ("store.js", "// not implemented yet"),
    ("Store.cs", "// 파일 저장은 지원 안 됨, 대신 콘솔 출력"),
])
def test_D4_source_markers_block_done(tmp_path, name, line):
    (tmp_path / name).write_text(f"{line}\nx = 1\n", encoding="utf-8")
    answer = report("완료", f"| 1 | 저장 | {name} | V4 | 사람 확인 |\n")
    rv = evidence.review(answer, "t", [], tmp_path, used_any_tool=True, dev_report=True)
    assert any(name in f.text for f in rv.blocking)


def test_D4_rule_is_in_dev_standard():
    from agent.prompts import load_prompts
    assert "다른 동작으로 바꿨으면 그 요구는 V0" in load_prompts().blocks["dev_standard"].text


# ───────────────────────── D5. 셸로 고친 뒤 같은 실행

def test_D5_run_edit_by_shell_rerun_is_not_a_repeat(settings, profile):
    test = lambda: call("run_python", code="print(open('a.txt').read() if __import__('os').path.exists('a.txt') else 0)")  # noqa: E731
    edit = lambda n: call("run_python", code=f"open('a.txt', 'w').write('{n}')")   # noqa: E731
    script = [reply(test()), reply(edit(1)), reply(test()), reply(edit(2)), reply(test()), reply(edit(3)),
              reply(test()), reply(content="끝")]
    events = run(Agent(settings, profile, llm=FakeLLM(script), approver=lambda r: True, max_steps=0))
    assert events[-1].status == "done" and not any("반복" in r.output for r in results(events))


def test_D5_true_repeats_of_a_command_are_still_caught(settings, profile):
    same = lambda: call("run_python", code="print(1)")   # noqa: E731
    events = run(Agent(settings, profile, llm=FakeLLM([reply(same())] * 3 + [reply(content="정리")]),
                       approver=lambda r: True, max_steps=0))
    r = results(events)
    assert "반복 경고" in r[1].output and "반복 감지" in r[2].output and events[-1].status == "loop"


# ───────────────────────── D6. 기록 압축 표시를 파일 내용으로

def test_D6_compaction_note_is_never_written(settings, profile, ws):
    profile.auto_approve_writes = True
    leaked = "def main():\n    " + ARG_NOTE.format(n=5120) + "\n"
    events = run(Agent(settings, profile, llm=FakeLLM([reply(call("write_file", path="app.py", content=leaked)),
                                                        reply(content="끝")])))
    r = results(events)[0]
    assert not r.ok and "압축" in r.output and not (ws / "app.py").exists()


# ───────────────────────── D7. 폴더 통째 삭제는 '모두 승인' 중에도 확인

def test_D7_project_folder_delete_always_asks(settings, ws):
    (ws / "proj").mkdir()
    (ws / "proj" / "README.md").write_text("x", encoding="utf-8")
    asked: list[ApprovalRequested] = []
    profile = coder()
    llm = FakeLLM([reply(call("run_command", command="Remove-Item -Recurse -Force proj")), reply(content="끝")])
    events = run(Agent(settings, profile, llm=llm, approver=lambda r: asked.append(r) or False))
    assert asked and asked[0].always_ask and (ws / "proj" / "README.md").exists()
    assert [e for e in events if isinstance(e, Note) and e.kind == "danger"]
    assert not RunHandle(agent=None, auto="all").covers(asked[0])
    plain = ApprovalRequested(1, "c", "run_command", {"command": "dir"}, "실행")
    assert RunHandle(agent=None, auto="all").covers(plain)


@pytest.mark.parametrize("tool,args,flagged", [
    ("run_command", {"command": "Remove-Item -Recurse -Force proj"}, True),
    ("run_command", {"command": "cmd /c rmdir /s /q proj"}, True),
    ("run_command", {"command": "rm -r -fo ."}, True),
    ("run_command", {"command": "Remove-Item -Recurse -Force C:\\Users"}, True),
    ("run_python", {"code": "import shutil; shutil.rmtree('proj')"}, True),
    ("run_command", {"command": "Remove-Item -Recurse -Force proj\\__pycache__"}, False),
    ("run_command", {"command": "Remove-Item -Recurse -Force _scratch"}, False),
    ("run_command", {"command": "Remove-Item proj\\old.txt"}, False),
    ("run_command", {"command": "Get-ChildItem proj -Recurse"}, False),
])
def test_D7_destructive_reason(ws, tool, args, flagged):
    (ws / "proj" / "__pycache__").mkdir(parents=True)
    (ws / "proj" / "package.json").write_text("{}", encoding="utf-8")
    (ws / "_scratch").mkdir()
    assert bool(destructive_reason(tool, args, ws)) is flagged


# ───────────────────────── D8. 인자에 섞인 호출 문법

def test_D8_leaked_call_syntax_points_to_format(settings, profile):
    broken = {"path": "a.py</parameter>\n<parameter=content>\nprint(1)"}
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="write_file", arguments=json.dumps(broken)))
    assert "호출 형식이 깨졌습니다" in _parse_tool_call(tc).error
    tc.function.arguments = broken   # 서버가 이미 dict 로 준 경우
    assert "호출 형식이 깨졌습니다" in _parse_tool_call(tc).error
    profile.auto_approve_writes = True
    events = run(Agent(settings, profile, llm=FakeLLM([reply(call("write_file", **broken)), reply(content="끝")])))
    out = results(events)[0].output
    assert "호출 형식" in out and "':'" not in out


@pytest.mark.parametrize("content", ["<p>parameter=1</p><function>", "<parameter>a</parameter>\n</config>\n",
                                     "<function>f</function> 설명"])
def test_D8_normal_markup_in_content_is_fine(content):
    ok = {"path": "index.html", "content": content}
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="write_file", arguments=json.dumps(ok)))
    assert not _parse_tool_call(tc).error


def test_D8_trailing_closing_tag_is_caught():
    assert "호출 형식" in leaked_call_syntax({"path": "src/a.py\n</parameter>"})


# ───────────────────────── D9. 사용자 프로젝트가 기준(rootdir)이 된다

def test_D9_repo_has_no_pytest_config_that_user_projects_inherit():
    for name in ("pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"):
        f = ROOT / name
        assert not f.exists() or "pytest" not in f.read_text(encoding="utf-8"), name


@pytest.mark.parametrize("own_config", [False, True])
def test_D9_project_inside_repo_is_its_own_rootdir(own_config):
    project = ROOT / "data" / f"_rootdir_check_{uuid.uuid4().hex[:8]}"
    try:
        (project / "tests").mkdir(parents=True)
        (project / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        if own_config:
            (project / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n", encoding="utf-8")
        r = subprocess.run([sys.executable, "-m", "pytest", "--co", "-p", "no:cacheprovider"], cwd=project,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        rootdir = next(line for line in r.stdout.splitlines() if line.startswith("rootdir:"))
        assert rootdir.split(":", 1)[1].strip().split(",")[0].lower() == str(project).lower()
        assert "test_x" in r.stdout and r.returncode == 0
    finally:
        shutil.rmtree(project, ignore_errors=True)


# ───────────────────────── D10. 의존성·정리 주장 대조 (알려 주기만)

def test_D10_dependency_version_and_cleanup_claims_are_checked(tmp_path):
    ledger = [evidence.Step("run_command", {"command": "pip install requests"}, True,
                            "exit_code: 0\nSuccessfully installed requests-2.31.0 urllib3-2.2.1")]
    answer = report("부분 완료", "| 1 | 가져오기 | - | V0 | - |\n",
                    extra="\n### 의존성\n- requests 2.32.0\n- urllib3 2.2.1\n### 정리\n- `build/` 폴더 삭제\n")
    rv = evidence.review(answer, "t", ledger, tmp_path, used_any_tool=True, dev_report=True)
    texts = [f.text for f in rv.findings]
    assert any("requests 2.32.0" in t and "2.31.0" in t for t in texts)
    assert not any("urllib3" in t for t in texts)
    assert any("build" in t and "기록에 없습니다" in t for t in texts)
    assert not rv.blocking   # 알려 주기만 하고 '완료' 판정은 막지 않는다

    ledger.append(evidence.Step("run_command", {"command": "Remove-Item -Recurse build"}, True, "exit_code: 0"))
    rv = evidence.review(answer, "t", ledger, tmp_path, used_any_tool=True, dev_report=True)
    assert not any("build" in f.text for f in rv.findings)


# ───────────────────────── D11. 사용자가 쓰지 않은 문자

def test_D11_han_characters_are_noted_but_code_is_ignored():
    assert evidence.check_language("구현을 完了했습니다", "합계 계산")
    assert not evidence.check_language("구현했습니다\n```\nprint('完了')\n```", "합계 계산")
    assert not evidence.check_language("完了", "中文으로 답해줘")


def test_D11_checked_in_any_answer_not_only_dev_reports(settings, ws):
    events = run(Agent(settings, load_profile("general"), llm=FakeLLM([reply(content="pygame (音视频 처리) 를 쓰면 됩니다")])),
                 task="파이썬으로 소리 내는 법")
    final = events[-1]
    assert final.status == "done" and "한자" in final.answer and "音视频" in final.answer


def test_D11_note_is_appended_to_final_answer(settings, ws):
    profile = coder()
    answer = report("부분 완료", "| 1 | 합계 | calc/core.py | V0 | 아직 |\n", extra="\n남은 것: 테스트 作成\n")
    events = run(Agent(settings, profile, llm=FakeLLM(write_all(PROJECT) + [reply(content=answer)] * 3),
                       approver=lambda r: True, max_steps=0))
    final = events[-1]
    assert final.status == "partial" and "(참고)" in final.answer and "한자" in final.answer
