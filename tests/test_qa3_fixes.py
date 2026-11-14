"""QA 3차(R11~R15) 결함 E1~E10 의 완료 기준을 가짜 모델로 확인한다.
특정 과제(동영상 플레이어)와 무관한 일반 시나리오만 쓰고, 언어별로 같은 규칙이 통하는지도 본다."""

from datetime import datetime
from pathlib import Path

import pytest

from agent import codecheck, evidence, session
from agent.boundary import Boundary
from agent.context import ARG_NOTE
from agent.events import Note, ToolResult
from agent.known_errors import KnownErrors
from agent.loop import Agent, looks_unfinished
from agent.profiles import load_profile
from agent.tools.shell import LAUNCH_HINT, ps51_compat, run_command
from tests.conftest import FakeLLM, call, reply
from tests.test_server import client, post, wait_done  # noqa: F401  (client 픽스처)

TABLE = "| # | 요구 | 구현 위치 | 등급 | 근거 |\n|---|---|---|---|---|\n"
TASK = "메모장 프로그램 만들어줘. (다른 대화에서 여러 번 시켰는데 저장이 안 돼서 다시 부탁해)"


def run(agent, task=TASK):
    return list(agent.run(task))


def results(events):
    return [e for e in events if isinstance(e, ToolResult)]


def notes(events, kind):
    return [e for e in events if isinstance(e, Note) and e.kind == kind]


def coder():
    p = load_profile("coder")
    p.auto_approve_writes = True
    return p


def report(result, *rows, extra=""):
    return f"## 결과: {result}\n### 스택\nPython 3.12\n### 요구사항별 검증\n" + TABLE + "".join(rows) + extra


def write(base: Path, files: dict[str, str | bytes]):
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content) if isinstance(content, bytes) else p.write_text(content, encoding="utf-8")


# ───────────────────────── E1. 대화 작업 폴더 밖

def test_E1_folder_name_stops_at_first_sentence_and_word_boundary():
    name = session.folder_name(TASK, datetime(2026, 11, 6, 11, 25))
    assert name == "2026-11-06_1125_메모장 프로그램 만들어줘"
    long = session.folder_name("아주 긴 요청 " + "단어 " * 30, datetime(2026, 11, 6, 11, 25))
    assert len(long) <= 16 + 40 and not long.endswith(" ") and long.split("_", 2)[2].split()[-1] == "단어"


@pytest.fixture
def chat(settings, ws):
    """세션 폴더 이름이 예전처럼 문장 부호·잘린 글자를 담은 경우 (이어서 하는 대화)."""
    folder = ws / "2026-11-06_1125_메모장 프로그램 만들어줘. (다른 대화에서 여러 번 시켰는데 저"
    folder.mkdir()
    return folder


def test_E1_sibling_folder_by_command_is_refused_and_not_created(settings, ws, chat):
    sibling = ws / "2026-11-06_1125_메모장 프로그램 만들어줘"
    script = [reply(call("run_command", command=f"New-Item -ItemType Directory -Path '{sibling}' -Force")),
              reply(call("run_command", command=f"cd '{sibling}'; python -m venv .venv")),
              reply(content="끝")]
    events = run(Agent(settings, coder(), llm=FakeLLM(script), approver=lambda r: True, workdir=chat))
    r = results(events)
    assert not r[0].ok and "workspace 에 없는 폴더" in r[0].output and "상대 경로" in r[0].output
    assert chat.name in r[0].output and not sibling.exists()
    assert len(notes(events, "outside")) >= 1


def test_E1_write_outside_is_refused_inside_is_fine(settings, ws, chat):
    sibling = ws / "2026-11-06_1125_메모장 프로그램 만들어줘"
    script = [reply(call("write_file", path=str(sibling / "app" / "main.py"), content="print(1)")),
              reply(call("write_file", path="app/main.py", content="print(1)")),
              reply(content="끝")]
    events = run(Agent(settings, coder(), llm=FakeLLM(script), approver=lambda r: True, workdir=chat))
    r = results(events)
    assert not r[0].ok and "작업 폴더 밖" in r[0].output and "잘린 것 같습니다" in r[0].output
    assert r[1].ok and (chat / "app" / "main.py").exists() and not sibling.exists()


def test_E1_folder_created_by_code_is_reported(settings, ws, chat):
    script = [reply(call("run_python", code="import os; os.makedirs('../stray', exist_ok=True)")), reply(content="끝")]
    events = run(Agent(settings, coder(), llm=FakeLLM(script), approver=lambda r: True, workdir=chat))
    out = results(events)[0].output
    assert "작업 폴더 밖에 새로 생겼습니다: 'stray'" in out and "Move-Item" in out
    assert notes(events, "outside")


def test_E1_existing_folder_named_in_request_and_reading_other_folders_are_allowed(settings, ws, chat):
    (ws / "shared").mkdir()
    (ws / "data").mkdir()
    (ws / "data" / "in.txt").write_text("x", encoding="utf-8")
    script = [reply(call("write_file", path=str(ws / "shared" / "out.txt"), content="ok")),
              reply(call("run_command", command=f"Get-Content '{ws / 'data' / 'in.txt'}'")),
              reply(content="끝")]
    events = run(Agent(settings, coder(), llm=FakeLLM(script), approver=lambda r: True, workdir=chat),
                 task="결과를 workspace 의 shared 폴더에 out.txt 로 저장해줘")
    assert all(r.ok for r in results(events)) and (ws / "shared" / "out.txt").exists()


def test_E1_no_session_folder_means_no_boundary(settings, ws):
    script = [reply(call("write_file", path=str(ws / "other" / "a.txt"), content="ok")), reply(content="끝")]
    events = run(Agent(settings, coder(), llm=FakeLLM(script), approver=lambda r: True))
    assert results(events)[0].ok


# ───────────────────────── E2. 끊긴 답 · 보고 형식이 아닌 답

@pytest.mark.parametrize("text,finish,expected", [
    ("V1 정적 검증이 통과했습니다. 이제 V2 단", "stop", True),
    ("좋습니다! 컴파일 오류가 없습니다. 이제 V2 단", "", True),
    ("Now let me run the tests and", "stop", True),
    ("아무 문장", "length", True),
    ("", "tool_calls", True),
    ("파일은 3개입니다.", "stop", False),
    ("합계: 42", "stop", False),
    ("context_length 값은 16384", "stop", False),
    ("이제 모든 작업이 끝났습니다", "stop", False),
    ("## 결과: 부분 완료\n| 1 | a | b | V1 | - |", "stop", False),
])
def test_E2_looks_unfinished(text, finish, expected):
    assert looks_unfinished(text, finish) is expected


def test_E2_cut_off_answer_after_gate_is_not_done(settings, ws):
    cut = "V1 정적 검증이 통과했습니다. 이제 V2 단"
    script = [reply(call("write_file", path="app/main.py", content="print(1)"))] + [reply(content=cut)] * 6
    events = run(Agent(settings, coder(), llm=FakeLLM(script), approver=lambda r: True, max_steps=0))
    final = events[-1]
    assert len(notes(events, "unfinished")) == 2
    assert final.status != "done" and "끊겼습니다" in final.answer


def test_E2_dev_run_ending_without_report_is_partial(settings, ws):
    files = {"calc/README.md": "# c", "calc/requirements.txt": "", "calc/.gitignore": ".venv/\n",
             "calc/core.py": "def add(a, b):\n    return a + b\n", "calc/tests/test_core.py": "from core import add\n"}
    script = [reply(call("write_file", path=p, content=c)) for p, c in files.items()]
    script += [reply(content="다 만들었습니다.")] * 4
    events = run(Agent(settings, coder(), llm=FakeLLM(script), approver=lambda r: True, max_steps=0))
    assert len(notes(events, "gate")) == 2 and events[-1].status == "partial"


def test_E2_plain_answers_still_done(settings, ws):
    for answer in ("파일은 3개입니다.", "합계: 42"):
        events = run(Agent(settings, load_profile("general"), llm=FakeLLM([reply(content=answer)])), task="질문")
        assert events[-1].status == "done" and events[-1].answer == answer


def test_E2_finish_reason_is_recorded(settings, ws):
    r = reply(content="답입니다.")
    r.finish_reason = "stop"
    events = run(Agent(settings, load_profile("general"), llm=FakeLLM([r])), task="질문")
    assert next(e for e in events if type(e).__name__ == "LLMCall").finish_reason == "stop"


# ───────────────────────── E3. 점검 메시지에 '어디를 봤는지'

def test_E3_gate_names_checked_folder_and_outside_work(settings, ws, chat):
    sibling = ws / "2026-11-06_1125_메모장 프로그램 만들어줘"
    script = [reply(call("write_file", path=str(sibling / "notes" / "app.py"), content="print(1)")),
              reply(call("write_file", path="notes/app.py", content="print(1)")),
              reply(content=report("완료", "| 1 | 저장 | notes/app.py | V1 | 문법 |\n"))] * 1
    script += [reply(content=report("부분 완료", "| 1 | 저장 | notes/app.py | V1 | 문법 |\n"))] * 3
    events = run(Agent(settings, coder(), llm=FakeLLM(script), approver=lambda r: True, workdir=chat, max_steps=0))
    gate = notes(events, "gate")[0].injected
    assert f"점검한 작업 폴더: '{chat.name}'" in gate and "작업 폴더 밖에서 한 일" in gate and sibling.name in gate


def test_E3_boundary_summary_is_empty_when_nothing_outside(ws):
    folder = ws / "s"
    folder.mkdir()
    assert Boundary(ws, folder, "t").summary() == ""


# ───────────────────────── E4. 껍데기 구현 · import 확인뿐인 근거

PY_STUB = '''class Player:
    def __init__(self, path):
        self.path = path
        self.on = False

    def start(self):
        """시작"""
        print("시작합니다")
        self.on = True

    def jump(self, sec):
        print(f"{sec} 초 이동")

    def level(self, v):
        if 0 <= v <= 1:
            self.v = v
            print("설정")
        else:
            print("범위 밖")
'''
PY_REAL = '''class Counter:
    def __init__(self):
        self.n = 0

    def add(self, k):
        self.n = max(0, self.n + k)
        return self.n

    def save(self, path):
        with open(path, "w") as f:
            f.write(str(self.n))
'''


@pytest.mark.parametrize("name,source,stubs", [
    ("p.py", PY_STUB, {"start", "jump", "level"}),
    ("c.py", PY_REAL, set()),
    ("p.js", "class P {\n  start() {\n    console.log('start');\n    this.on = true;\n  }\n"
             "  jump(s) {\n    this.t = Math.min(this.d, this.t + s);\n  }\n}\n", {"start"}),
    ("P.cs", "public class P {\n  public void Start() {\n    Console.WriteLine(\"start\");\n    _on = true;\n  }\n"
             "  public void Save(string p) {\n    File.WriteAllText(p, _text);\n  }\n"
             "  public void Export() { throw new NotImplementedException(); }\n}\n", {"Start", "Export"}),
])
def test_E4_stub_functions_by_language(tmp_path, name, source, stubs):
    (tmp_path / name).write_text(source, encoding="utf-8")
    assert codecheck.stub_functions(tmp_path / name)[1] == stubs


def _review(answer, ledger, workdir):
    return evidence.review(answer, "t", ledger, workdir, used_any_tool=True, dev_report=True)


def test_E4_stub_implementation_is_not_v3(tmp_path):
    write(tmp_path, {"app/player.py": PY_STUB, "app/tests/test_player.py":
                     "from player import Player\np = Player('x')\np.start()\nassert p.on\n"})
    ok = [evidence.Step("run_command", {"command": "python -m pytest app/tests"}, True, "exit_code: 0\n1 passed")]
    v3 = _review(report("완료", "| 1 | 시작 | app/player.py | V3 | `python -m pytest app/tests` 1 passed |\n"), ok, tmp_path)
    assert any("껍데기" in f.text and f.blocks_done for f in v3.findings)
    v2 = _review(report("부분 완료", "| 1 | 시작 | app/player.py | V2 | `python -m pytest app/tests` 1 passed |\n"), ok, tmp_path)
    assert any("껍데기" in f.text and not f.blocks_done for f in v2.findings)


def test_E4_real_logic_is_not_flagged(tmp_path):
    write(tmp_path, {"app/counter.py": PY_REAL, "app/tests/test_counter.py":
                     "from counter import Counter\nc = Counter()\nassert c.add(2) == 2\n"})
    ok = [evidence.Step("run_command", {"command": "python -m pytest app/tests"}, True, "exit_code: 0\n1 passed")]
    rv = _review(report("부분 완료", "| 1 | 더하기 | app/counter.py:add() | V2 | `python -m pytest app/tests` 1 passed |\n"),
                 ok, tmp_path)
    assert not rv.findings


def test_E4_import_only_evidence_is_rejected(tmp_path):
    write(tmp_path, {"app/player.py": PY_REAL})
    cmd = "python -c \"import sys; sys.path.insert(0, 'app'); from player import Counter; print('Import OK')\""
    ok = [evidence.Step("run_command", {"command": cmd}, True, "exit_code: 0\n[stdout]\nImport OK")]
    rv = _review(report("부분 완료", f"| 1 | 열기 | app/player.py | V3 | `{cmd}` → Import OK |\n"), ok, tmp_path)
    assert any("import 확인뿐" in f.text for f in rv.blocking)


# ───────────────────────── E5. 일부 요구만 시험한 테스트

APP = '''def fmt(ms):
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}"


class App:
    def open_file(self, path):
        self.text = open(path, encoding="utf-8").read()

    def seek(self, sec):
        self.pos = max(0, self.pos + sec * 1000)

    def set_level(self, v):
        self.level = min(1.0, max(0.0, v))
'''
TEST_FMT = "from app import fmt\n\ndef test_fmt():\n    \"\"\"seek 과는 무관\"\"\"\n    assert fmt(60000) == '01:00'\n"
PASS = [evidence.Step("run_command", {"command": "python -m pytest -q"}, True, "exit_code: 0\n1 passed")]


def test_E5_row_whose_function_the_test_never_calls_is_flagged(tmp_path):
    write(tmp_path, {"proj/app.py": APP, "proj/tests/test_app.py": TEST_FMT})
    rows = ("| 1 | 시간 표시 | app.py:fmt() | V2 | `python -m pytest -q` 1 passed |\n"
            "| 2 | 이동 | app.py:seek() | V2 | `python -m pytest -q` 1 passed |\n")
    rv = _review(report("부분 완료", rows), PASS, tmp_path)
    flagged = [f.text for f in rv.blocking]
    assert any("#2" in t and "seek" in t and "부르지 않습니다" in t for t in flagged)
    assert not any("#1" in t for t in flagged)


def test_E5_many_rows_on_one_narrow_test_are_flagged(tmp_path):
    write(tmp_path, {"proj/app.py": APP, "proj/tests/test_app.py": TEST_FMT})
    rows = "".join(f"| {i} | 요구{i} | app.py | V2 | `python -m pytest -q` 1 passed |\n" for i in range(1, 5))
    rv = _review(report("부분 완료", rows), PASS, tmp_path)
    assert any("요구 4개" in f.text and "1개(fmt)" in f.text for f in rv.blocking)


@pytest.mark.parametrize("location,symbols", [
    ("main.py:open_file()", ["open_file"]), ("main.py:set_volume(), on_volume_changed()", ["set_volume", "on_volume_changed"]),
    ("player.py::Player.play", ["play"]), ("src/Player.cs — Play(), Pause()", ["Play", "Pause"]),
    ("src/player/player.py", []), ("-", []), ("app.js#render", ["render"]),
])
def test_E5_location_symbols(location, symbols):
    assert codecheck.location_symbols(location) == symbols


def test_E5_js_and_cs_tests_are_matched_by_called_names(tmp_path):
    write(tmp_path, {"web/src/clock.js": "export function fmt(ms) { return String(ms); }\nexport function seek(s) { return s; }\n",
                     "web/test/clock.test.js": "import { fmt } from '../src/clock.js';\n// seek 는 나중에\ntest('x', () => fmt(1));\n"})
    run_js = [evidence.Step("run_command", {"command": "cd web; node --test"}, True, "exit_code: 0\n# pass 1")]
    rv = _review(report("부분 완료", "| 1 | 이동 | web/src/clock.js:seek() | V2 | `node --test` pass 1 |\n"), run_js, tmp_path)
    assert any("seek" in f.text for f in rv.blocking)


# ───────────────────────── E6. 고친 오류의 재발

def test_E6_known_errors_patterns_and_false_positive_guards(tmp_path):
    k = KnownErrors()
    k.learn("ImportError: cannot import name 'PlayerState' from 'medialib.core' (x.pyd)\n"
            "AttributeError: type object 'medialib.Player' has no attribute 'VideoSurface'\n"
            "AttributeError: 'Player' object has no attribute 'state'\n"
            "error CS0117: 'Media' does not contain a definition for 'SetAudioVolume'\n")
    py = tmp_path / "a.py"
    assert {b.name for b in k.check(py, "from medialib.core import Player, PlayerState\nx = Player.VideoSurface\n", None)} == \
        {"PlayerState", "VideoSurface"}
    assert not k.check(py, "p = Player()\nprint(p.state)\n", None)            # 흔한 이름은 단독으로 보지 않는다
    assert not k.check(py, "class PlayerState:\n    pass\n", None)            # 직접 정의하면(고친 것) 뺀다
    assert not k.check(tmp_path / "notes.md", "PlayerState", None)            # 코드 파일만
    assert [b.name for b in k.check(tmp_path / "A.cs", "Media.SetAudioVolume(1);", None)] == ["SetAudioVolume"]


def test_E6_reintroduced_name_is_flagged_on_write(settings, ws, profile):
    profile.auto_approve_writes = True
    err = "import sys; sys.stderr.write(\"ImportError: cannot import name 'PlayerState' from 'medialib'\\n\"); sys.exit(1)"
    script = [reply(call("run_python", code=err)),
              reply(call("write_file", path="main.py", content="from medialib import Player\n")),
              reply(call("write_file", path="main.py", content="from medialib import Player, PlayerState\n")),
              reply(content="끝")]
    events = run(Agent(settings, profile, llm=FakeLLM(script), approver=lambda r: True))
    r = results(events)
    assert "다시 들어갔습니다" not in r[1].output
    assert "'PlayerState'" in r[2].output and "고친 것을 되돌리지" in r[2].output
    assert notes(events, "regression")


def test_E6_errors_from_the_previous_run_carry_over(client):  # noqa: F811
    err = "import sys; sys.stderr.write(\"ImportError: cannot import name 'PlayerState' from 'medialib'\\n\")"
    client.script[:] = [reply(call("run_python", code=err)), reply(content="고쳤습니다.")]
    first = post(client, "/api/v1/runs", {"task": "오류 고치기", "profile": "coder", "auto_approve": "all"}).json()["run_id"]
    wait_done(client, first)
    client.script[:] = [reply(call("write_file", path="main.py", content="from medialib import PlayerState\n")),
                        reply(content="다시 썼습니다.")]
    second = post(client, "/api/v1/runs", {"task": "다시", "profile": "coder", "auto_approve": "all",
                                            "parent_run_id": first}).json()["run_id"]
    events = wait_done(client, second)["events"]
    out = next(e for e in events if e["type"] == "ToolResult")["output"]
    assert "'PlayerState'" in out and "다시 들어갔습니다" in out


# ───────────────────────── E7. 실제 형식이 아닌 테스트 데이터

@pytest.mark.parametrize("name,data,fake", [
    ("v.mp4", b"\x00" * 100, True), ("v.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20, False),
    ("a.wav", b"RIFF\x24\x00\x00\x00WAVEfmt ", False), ("a.wav", b" " * 40, True),
    ("i.png", b"not a png", True), ("i.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, False),
    ("d.pdf", b"", True), ("notes.txt", b"\x00\x00", False),
])
def test_E7_fake_reason(tmp_path, name, data, fake):
    (tmp_path / name).write_bytes(data)
    assert bool(codecheck.fake_reason(tmp_path / name)) is fake


def test_E7_test_using_fake_media_is_not_evidence(tmp_path):
    write(tmp_path, {"proj/app.py": APP, "proj/tests/sample.mp4": b"\x00" * 100,
                     "proj/tests/test_app.py": "from app import App\n\ndef test_open():\n    App().open_file('tests/sample.mp4')\n"})
    rv = _review(report("부분 완료", "| 1 | 열기 | app.py:open_file() | V2 | `python -m pytest -q` 1 passed |\n"), PASS, tmp_path)
    assert any("sample.mp4" in f.text and "실제 .mp4 형식이 아닙니다" in f.text for f in rv.blocking)


def test_E7_writing_binary_extension_as_text_is_warned(settings, ws, profile):
    profile.auto_approve_writes = True
    events = run(Agent(settings, profile, llm=FakeLLM([reply(call("write_file", path="t.mp4", content=" " * 100)),
                                                        reply(content="끝")])))
    assert "진짜 .mp4 파일이 아닙니다" in results(events)[0].output and notes(events, "fake_file")


# ───────────────────────── E8. PowerShell 5.1 문법

@pytest.mark.parametrize("cmd,expected", [
    ("cd app && python -m pytest", "cd app; if ($?) { python -m pytest }"),
    ("a && b && c", "a; if ($?) { b; if ($?) { c } }"),
    ("a || b", "a; if (-not $?) { b }"),
    ("a && b || c", "a && b || c"),
    ('python -c "print(1 && 2)"', 'python -c "print(1 && 2)"'),
    ("mkdir -p src/app tests", "New-Item -ItemType Directory -Force -Path 'src/app', 'tests' | Out-Null"),
    ("mkdir src", "mkdir src"),
    ("rm -rf build", "Remove-Item -Recurse -Force -Path 'build'"),
    ("ls -la", "Get-ChildItem -Force"),
    ("git status; mkdir -p x/y", "git status; New-Item -ItemType Directory -Force -Path 'x/y' | Out-Null"),
])
def test_E8_ps51_compat(cmd, expected):
    assert ps51_compat(cmd)[0] == expected


def test_E8_translated_chain_really_runs_and_stops_on_failure(ctx):
    ok = run_command(ctx, command="echo first && echo second")
    assert ok.startswith("exit_code: 0") and "first" in ok and "second" in ok and "바꿔 실행했습니다" in ok
    stop = run_command(ctx, command="cmd /c exit 3 && echo never")
    assert "[stdout]" not in stop   # 앞 명령이 실패하면 뒤 명령은 돌지 않는다 (bash 의 && 와 같은 뜻)
    ctx.sandbox.cwd.mkdir(parents=True, exist_ok=True)
    made = run_command(ctx, command="mkdir -p one/two three")
    assert made.startswith("exit_code: 0") and (ctx.sandbox.cwd / "one" / "two").is_dir() and (ctx.sandbox.cwd / "three").is_dir()


# ───────────────────────── E9. 보고 내용과 실행 기록

def test_E9_version_in_stack_line_is_compared_but_minimums_are_not(tmp_path):
    ledger = [evidence.Step("run_command", {"command": "pip install medialib"}, True,
                            "exit_code: 0\nSuccessfully installed medialib-6.12.0")]
    answer = ("## 결과: 부분 완료\n### 스택 / ### 의존성\n- 스택: Python 3.12 + medialib 6.7.2 (이미 설치됨)\n"
              "- 의존성: medialib>=6.4.0\n### 요구사항별 검증\n" + TABLE + "| 1 | a | - | V0 | - |\n")
    texts = [f.text for f in _review(answer, ledger, tmp_path).findings]
    assert any("medialib 6.7.2" in t and "6.12.0" in t for t in texts)
    assert not any("6.4.0" in t for t in texts)


def test_E9_report_ended_by_step_limit_is_also_checked(settings, ws):
    settings.limits.max_steps = 1
    profile = coder()
    rep = report("부분 완료", "| 1 | 저장 | app.py | V2 | pytest 3 passed |\n")
    events = run(Agent(settings, profile, llm=FakeLLM([reply(call("list_dir", path=".")), reply(content=rep)]),
                       approver=lambda r: True))
    final = events[-1]
    assert final.status == "max_steps" and "실행 기록 점검" in final.answer


def test_E9_compaction_note_says_the_file_is_intact():
    assert "그대로" in ARG_NOTE and "기록 압축 표시" in ARG_NOTE


# ───────────────────────── E10. Start-Process 의 종료 코드

def test_E10_start_process_output_has_hint(ctx):
    out = run_command(ctx, command="Start-Process -FilePath cmd -ArgumentList '/c','exit 0' -WindowStyle Hidden -Wait")
    assert out.startswith("exit_code: 0") and LAUNCH_HINT in out


def test_E10_launch_only_evidence_is_not_v3(tmp_path):
    write(tmp_path, {"proj/app.py": APP})
    launch = [evidence.Step("run_command", {"command": "Start-Process python -ArgumentList 'proj/app.py' -Wait"}, True,
                            "exit_code: 0")]
    rv = _review(report("완료", "| 1 | 실행 | proj/app.py | V3 | `Start-Process python -ArgumentList 'proj/app.py' -Wait` exit 0 |\n"),
                 launch, tmp_path)
    assert any("Start-Process" in f.text and "동작 확인이 아닙니다" in f.text for f in rv.blocking)
