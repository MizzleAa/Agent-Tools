"""QA R01~R05 결함(A1~A6, B1~B5)의 완료 기준을 가짜 모델로 확인한다. 특정 과제(동영상 플레이어)와 무관한 일반 시나리오만 쓴다."""

import os
import shutil
import subprocess

import httpx
import openai
import pytest

from agent import standards
from agent.config import ROOT
from agent.context import chars_of
from agent.events import Error, Note, ToolResult
from agent.loop import NUDGE_ACT, Agent, looks_like_plan
from agent.profiles import load_profile
from agent.tools import ToolError
from agent.tools.shell import TIMEOUT_HINT, check_install_policy, clean_clixml, run_command, run_python, user_python
from tests.conftest import FakeLLM, call, reply


def run(agent, task="작업", **kw):
    return list(agent.run(task, **kw))


# ───────────────────────── A1. 컨텍스트 한도

class TokenizingLLM:
    """실제 llama-server 처럼: 요청이 한도를 넘으면 400 으로 거절한다. 토큰 = 글자 × ratio 로 흉내."""
    model = "fake"

    def __init__(self, script, ctx, ratio):
        self.script, self.ctx, self.ratio = list(script), ctx, ratio
        self.max_seen, self.rejections = 0, 0

    def chat(self, messages, tools=None, think=None):
        tokens = int(chars_of(messages, tools) * self.ratio)
        if tokens > self.ctx:
            self.rejections += 1
            raise openai.BadRequestError(
                f"request ({tokens} tokens) exceeds the available context size ({self.ctx} tokens)", body=None,
                response=httpx.Response(400, request=httpx.Request("POST", "http://x")))
        self.max_seen = max(self.max_seen, tokens)
        r = self.script.pop(0) if self.script else reply(content="다 했습니다")
        r.prompt_tokens = tokens
        return r


@pytest.mark.parametrize("ctx,ratio", [(16384, 0.45), (16384, 0.8), (32768, 0.6)])
def test_A1_forty_big_writes_and_long_errors_never_exceed_context(settings, profile, ws, ctx, ratio):
    settings.backend.context_length = ctx
    profile.auto_approve_writes = True
    (ws / "build.log").write_text("\n".join(f"error E{i:04}: 줄 {i} 에서 무언가 실패했습니다 " + "x" * 40 for i in range(60)),
                                  encoding="utf-8")   # 4천 자 넘는 빌드·실행 오류 출력
    script = []
    for i in range(40):
        script.append(reply(call("write_file", path="app.src", content=f"// {i}\n" + ("code line\n" * 700))))   # 7KB
        script.append(reply(call("read_file", path="build.log", max_lines=60)))
    llm = TokenizingLLM(script + [reply(content="끝")], ctx, ratio)
    events = run(Agent(settings, profile, llm=llm, max_steps=0, max_seconds=0))
    final = events[-1]
    assert final.status == "done" and final.steps == 81, final.answer
    assert llm.max_seen <= ctx
    assert not [e for e in events if isinstance(e, Error)]


def test_A1_old_write_arguments_are_shrunk_but_recent_kept(settings, profile, ws):
    profile.auto_approve_writes = True
    settings.backend.context_length = 16384
    script = [reply(call("write_file", path=f"f{i}.txt", content=f"{i}" * 7000)) for i in range(12)] + [reply(content="끝")]
    agent = Agent(settings, profile, llm=TokenizingLLM(script, 16384, 0.5), max_steps=0)
    run(agent)
    args = [c["function"]["arguments"] for m in agent.messages for c in m.get("tool_calls") or []]
    assert any("기록을 줄이려고 생략" in a for a in args)                    # 오래된 7KB 인자는 줄었다
    assert len(args[-1]) > 6000                                             # 가장 최근 것은 그대로


# ───────────────────────── A2. 계획만 말하고 끝남

R01_ANSWER = "네, 프로그램을 만들어 드리겠습니다.\n\n먼저 현재 작업 폴더 구조를 확인하고, 필요한 라이브러리를 설치한 후 코드를 작성하겠습니다."


@pytest.mark.parametrize("text,expected", [
    (R01_ANSWER, True),
    ("I'll create the project folder first and then write the code.", True),
    ("A 방식과 B 방식 중 어느 쪽을 원하세요?", False),                      # 질문형
    ("GIL 은 한 번에 한 스레드만 파이썬 바이트코드를 실행하게 하는 잠금입니다.", False),
    ("저장했습니다. 추가로 원하시면 기능을 더 넣겠습니다.", False),          # 조건부 제안
    ("도움이 되었으면 좋겠습니다.", False),
])
def test_A2_plan_detection(text, expected):
    assert looks_like_plan(text) is expected


def test_A2_plan_only_answer_is_sent_back_and_then_executes(settings, profile, ws):
    llm = FakeLLM([reply(content=R01_ANSWER), reply(call("list_dir", path=".")), reply(content="폴더를 확인했습니다.")])
    events = run(Agent(settings, profile, llm=llm))
    assert [e.kind for e in events if isinstance(e, Note)] == ["act"]
    assert llm.calls[1]["messages"][-1]["content"] == NUDGE_ACT
    assert any(isinstance(e, ToolResult) for e in events)


def test_A2_never_acting_is_not_done(settings, profile):
    llm = FakeLLM([reply(content=R01_ANSWER)] * 3)
    final = run(Agent(settings, profile, llm=llm))[-1]
    assert final.status == "no_action" and final.status != "done"


def test_A2_question_answer_ends_as_before(settings, profile):
    llm = FakeLLM([reply(content="결과를 어떤 형식으로 저장할까요? CSV 와 엑셀 중에 골라 주세요?")])
    events = run(Agent(settings, profile, llm=llm))
    assert events[-1].status == "done" and len(llm.calls) == 1


# ───────────────────────── A3. 타임아웃 때 출력이 남는가 (Python, Node, .NET)

def _assert_output_kept(out, marker, sec=3):
    assert marker in out, out                                               # 멈추기 전까지 나온 출력이 남는다
    assert "시간 초과로 중지함" in out and "강제로 멈췄습니다" in out and "스스로 종료" in out   # 타임아웃의 뜻을 알려 준다
    assert TIMEOUT_HINT.format(sec=sec) in out


def test_A3_python_output_survives_timeout(ctx):
    _assert_output_kept(run_command(ctx, command="python -c \"print('A-py'); import time; time.sleep(999)\"", timeout_sec=3), "A-py")
    _assert_output_kept(run_python(ctx, code="print('B-py'); import time; time.sleep(999)", timeout_sec=3), "B-py")


@pytest.mark.skipif(not shutil.which("node"), reason="Node 없음")
@pytest.mark.skipif(os.environ.get("GITHUB_ACTIONS") == "true",
                    reason="CI 러너는 PowerShell 5.1 + node 첫 실행이 들쭉날쭉 10초를 넘긴다 — 같은 동작은 python 시험이 본다")
def test_A3_node_output_survives_timeout(ctx):
    # 시간 제한은 넉넉히 — 느린 PC(CI 러너)에서는 PowerShell 5.1 + node 가 뜨는 데만 3초를 넘기기도 한다
    _assert_output_kept(run_command(ctx, command="node -e \"console.log('A-node'); setInterval(() => {}, 1000)\"",
                                    timeout_sec=10), "A-node", sec=10)


@pytest.mark.skipif(not shutil.which("dotnet"), reason=".NET SDK 없음")
def test_A3_dotnet_output_survives_timeout(ctx, tmp_path):
    proj = tmp_path / "hang"
    subprocess.run(["dotnet", "new", "console", "-o", str(proj), "--force"], capture_output=True, check=True)
    (proj / "Program.cs").write_text('System.Console.WriteLine("A-dotnet"); System.Threading.Thread.Sleep(-1);', encoding="utf-8")
    subprocess.run(["dotnet", "build", str(proj), "-nologo", "-v", "q"], capture_output=True, check=True)
    exe = next(proj.rglob("hang.exe"))
    _assert_output_kept(run_command(ctx, command=f"& '{exe}'", timeout_sec=3), "A-dotnet")


# ───────────────────────── A4. 같은 파이썬

def test_A4_run_python_and_shell_python_are_the_same_interpreter(ctx):
    a = run_python(ctx, code="import sys; print(sys.executable)")
    b = run_command(ctx, command="python -c \"import sys; print(sys.executable)\"")
    pick = lambda out: out.split("[stdout]\n", 1)[1].strip().lower()   # noqa: E731
    assert pick(a) == pick(b) == user_python().lower()
    assert not pick(a).startswith(str(ROOT / ".venv").lower())   # 에이전트 자신의 .venv 가 아니다


# ───────────────────────── A5. 2>&1 · CLIXML · 종료 코드

def test_A5_success_with_stderr_redirect_is_reported_as_success(ctx):
    out = run_command(ctx, command="python -c \"import sys; sys.stderr.write('warn-line\\n'); print('ok-out')\" 2>&1")
    assert out.startswith("exit_code: 0") and "CLIXML" not in out and "ok-out" in out and "warn-line" in out


def test_A5_real_exit_code_is_kept(ctx):
    assert run_command(ctx, command="python -c \"import sys; sys.exit(3)\"").startswith("exit_code: 3")


def test_A5_clixml_is_turned_into_text():
    raw = ('#< CLIXML\r\n<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
           '<S S="Error">python : Traceback (most recent call last):_x000D__x000A_</S>'
           '<S S="Error">위치 줄:1 문자:129_x000D__x000A_</S><S S="Error">+ ... oding]::UTF8; python -c ..._x000D__x000A_</S>'
           '<S S="Error">ModuleNotFoundError: No module named &apos;x&apos;_x000D__x000A_</S></Objs>')
    text = clean_clixml(raw)
    assert "CLIXML" not in text and "<S" not in text and "ModuleNotFoundError: No module named 'x'" in text
    assert "위치 줄" not in text


# ───────────────────────── A6. 같은 원인의 실패

def _exec(agent, outputs, name="run_command"):
    notes = []
    for out in outputs:
        gen = agent._watch_failures(1, name, True, out)
        try:
            while True:
                notes.append(next(gen))
        except StopIteration as stop:
            last = stop.value
    return notes, last


def test_A6_R03_pattern_timeouts_interleaved_with_other_failures(settings, profile):
    agent = Agent(settings, profile, llm=FakeLLM([]))
    timeout = "exit_code: 15 (60초 시간 초과로 중지함)"
    attr = "exit_code: 1\n[stderr]\nAttributeError: 'X' object has no attribute 'y'"
    notes, last = _exec(agent, [timeout, "exit_code: 0\n[stdout]\nok", attr, timeout, attr, timeout])
    assert len(notes) == 1 and "시간 초과" in notes[0].message and "스스로 종료" in last


def test_A6_R04_pattern_build_failures_with_different_codes(settings, profile):
    agent = Agent(settings, profile, llm=FakeLLM([]))
    builds = [f"exit_code: 1\n[stdout]\nC:\\p\\Main.cs(12,5): error {code}: 무언가" for code in ("MC3000", "CS0246", "CS1061")]
    notes, last = _exec(agent, builds)
    assert len(notes) == 1 and "빌드 실패" in notes[0].message and "오류 코드" in last


# ───────────────────────── B1. 검증표 없는 완료 보고

TABLE = "| # | 요구 | 구현 위치 | 등급 | 근거 |\n|---|---|---|---|---|\n"


def test_B1_report_checks():
    assert any("결과" in p for p in standards.check_report("다 만들었습니다!"))
    assert any("검증' 표" in p for p in standards.check_report("## 결과: 완료\n잘 됩니다"))
    bad = "## 결과: 완료\n" + TABLE + "| 1 | 저장 | app.py | V2 | pytest 3 passed |\n"
    assert any("부분 완료" in p for p in standards.check_report(bad))
    good = "## 결과: 부분 완료\n" + TABLE + "| 1 | 저장 | app.py | V2 | pytest 3 passed |\n| 2 | 화면 | ui.py | V4 | 사람 확인 필요 |\n"
    assert standards.check_report(good) == []


def _dev_profile():
    p = load_profile("coder")
    p.auto_approve_writes = True
    return p


def test_B1_B2_gate_sends_back_then_partial_status(settings, ws):
    profile = _dev_profile()
    proper = ("## 결과: 부분 완료\n### 스택\nPython 3.12\n### 요구사항별 검증\n" + TABLE +
              "| 1 | 합계 계산 | calc/core.py | V0 | 테스트는 아직 실행 안 함 |\n")   # 실행 기록 없는 V2 는 근거 점검이 되돌린다 (QA2 D2, test_qa2_fixes)
    files = {"calc/README.md": "# calc", "calc/requirements.txt": "", "calc/.gitignore": ".venv/\n",
             "calc/core.py": "def add(a, b):\n    return a + b\n", "calc/tests/test_core.py": "from core import add\n"}
    script = [reply(call("write_file", path="main.py", content="print(1)")),      # 프로젝트 폴더 없이 바로 씀
              reply(content="완성했습니다!")]                                     # 검증표 없는 보고
    script += [reply(call("write_file", path=p, content=c)) for p, c in files.items()]
    script += [reply(call("run_command", command="Remove-Item main.py")), reply(content=proper)]
    agent = Agent(settings, profile, llm=FakeLLM(script), approver=lambda r: True, max_steps=0)
    events = run(agent)
    gates = [e for e in events if isinstance(e, Note) and e.kind == "gate"]
    assert len(gates) == 1 and "프로젝트 폴더" in gates[0].injected and "검증" in gates[0].injected
    assert events[-1].status == "partial"


# ───────────────────────── B2. 언어별 규격

def test_B2_language_standard_is_attached_once_when_development_starts(settings, ws):
    profile = _dev_profile()
    llm = FakeLLM([reply(call("write_file", path="app/main.py", content="print('hi')")),
                   reply(call("read_standard", name="python")), reply(content="## 결과: 실패\n" + TABLE + "| 1 | a | b | V0 | - |")])
    agent = Agent(settings, profile, llm=llm, approver=lambda r: True)
    events = run(agent)
    assert "언어별 규격: Python" in agent.messages[0]["content"]
    assert "언어별 규격: Python" not in llm.calls[0]["messages"][0]["content"]   # 처음부터 넣지는 않는다 (16k 절약)
    assert [e.kind for e in events if isinstance(e, Note)].count("standard") == 1
    assert "이미 시스템 지침에 붙어" in [e for e in events if isinstance(e, ToolResult)][1].output


@pytest.mark.parametrize("lang,files,missing", [
    ("python", {"p/README.md": "x", "p/requirements.txt": "", "p/.gitignore": "", "p/src/a.py": "", "p/tests/test_a.py": ""}, []),
    ("python", {"p/a.py": ""}, ["README", "의존성 선언", "자동 테스트", ".gitignore"]),
    ("dotnet", {"p/README.md": "x", "p/P.sln": "", "p/.gitignore": "", "p/src/P/P.csproj": "<Project/>",
                "p/tests/P.Tests/P.Tests.csproj": '<PackageReference Include="xunit" />'}, []),
    ("dotnet", {"p/src/P/P.csproj": "<Project/>"}, ["README", ".sln", "테스트 프로젝트"]),
    ("node-web", {"p/README.md": "x", "p/package.json": "{}", "p/.gitignore": "", "p/src/core.js": "",
                  "p/tests/core.test.js": ""}, []),
    ("node-web", {"p/index.html": ""}, ["README", "package.json", "자동 테스트"]),
])
def test_B2_project_checks_per_language(tmp_path, lang, files, missing):
    for rel, content in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content, encoding="utf-8")
    problems = " / ".join(standards.check_project(tmp_path, lang))
    assert all(m in problems for m in missing) and (problems == "" if not missing else True), problems
    assert standards.detect_from_folder(tmp_path) == lang


def test_B2_detect_language_from_calls():
    assert standards.detect_from_call("write_file", {"path": "x/Program.cs"}) == "dotnet"
    assert standards.detect_from_call("run_command", {"command": "npm init -y"}) == "node-web"
    assert standards.detect_from_call("run_command", {"command": "python -m venv .venv"}) == "python"
    assert standards.detect_from_call("write_file", {"path": "notes.md"}) is None


# ───────────────────────── B3·B4. 환경 확인 · 전역 설치 · 다운로드

@pytest.mark.parametrize("cmd", [
    "pip install requests", "python -m pip install requests", "py -m pip install x",
    ".venv\\Scripts\\python -m pip install --user x", "npm install -g typescript", "npm i --global x",
    "dotnet tool install -g dotnet-ef", "winget install foo",
    "Invoke-WebRequest https://example.com/x.zip -OutFile x.zip", "curl.exe -L -o a.exe https://x", "git clone https://x/y",
])
def test_B4_global_installs_and_downloads_are_blocked(ctx, cmd):
    with pytest.raises(ToolError):
        check_install_policy(ctx, cmd)


@pytest.mark.parametrize("install,check", [
    (".venv\\Scripts\\python -m pip install requests", "python -m pip list"),
    ('& ".venv\\Scripts\\python.exe" -m pip install -r requirements.txt', "python --version"),
    ("npm install lodash", "npm ls --depth=0"),
    ("dotnet add src/App package Newtonsoft.Json", "dotnet --list-sdks"),
])
def test_B3_local_install_needs_env_check_first(ctx, install, check):
    from agent.tools.shell import note_env_checks
    with pytest.raises(ToolError, match="환경 확인"):
        check_install_policy(ctx, install)
    note_env_checks(ctx, check)
    check_install_policy(ctx, install)   # 확인한 뒤에는 통과


def test_B4_policy_can_be_turned_off(ctx):
    ctx.settings.policy.block_global_install = False
    ctx.settings.policy.block_downloads = False
    ctx.settings.policy.require_env_check = False
    check_install_policy(ctx, "pip install x")
    check_install_policy(ctx, "Invoke-WebRequest https://x")


def test_B4_npm_install_from_package_json_needs_no_check(ctx):
    check_install_policy(ctx, "npm install")   # 패키지 이름 없는 npm install = package.json 대로 복원


# ───────────────────────── B5. 정리

def test_B5_stray_files_and_scratch_leftovers_are_flagged(tmp_path):
    (tmp_path / "p" / "_scratch").mkdir(parents=True)
    (tmp_path / "p" / "_scratch" / "probe.py").write_text("", encoding="utf-8")
    (tmp_path / "p" / "README.md").write_text("x", encoding="utf-8")
    (tmp_path / "test_once.py").write_text("", encoding="utf-8")
    problems = " / ".join(standards.check_project(tmp_path, None))
    assert "프로젝트 폴더 밖에 파일" in problems and "_scratch" in problems


def test_B5_dotnet_bin_obj_and_node_modules_are_not_flagged(tmp_path):
    files = {"p/README.md": "x", "p/P.sln": "", "p/.gitignore": "", "p/src/P/P.csproj": "<Project/>",
             "p/tests/T/T.csproj": "xunit", "p/src/P/bin/Debug/P.dll": "", "p/src/P/obj/x.json": "",
             "p/node_modules/a/index.js": ""}
    for rel, content in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content, encoding="utf-8")
    assert standards.check_project(tmp_path, "dotnet") == []
