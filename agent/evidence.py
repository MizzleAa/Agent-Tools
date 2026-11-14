"""최종 보고를 **이번 실행 기록**(도구 호출·결과)과 작업 폴더에 대조한다 — 근거 없는 주장 찾기.

개발 규격(기준/00_공통.md)의 검증 등급은 '무엇을 실행해 무엇을 봤는가'로만 매길 수 있다. 소형 모델은 보고 양식을
그럴듯하게 채우는 데는 능하지만, 그 내용이 실제로 일어났는지는 스스로 확인하지 못한다. 그래서 여기서 확인한다.

막는 것 (blocks_done=True — 이게 남아 있으면 '완료'로 기록하지 않는다)
- 도구를 한 번도 쓰지 않았는데 결과 줄·검증 표·파일 구조·'만들었다/실행했다'를 보고
- 검증 표의 '구현 위치' 파일이 작업 폴더에 없음
- V2·V3 근거로 적은 명령·테스트를 이번 실행에서 종료 코드 0 으로 돌린 기록이 없음
- V2·V3 근거 테스트가 프로젝트 자신의 코드를 import·실행하지 않음 (라이브러리만 시험)
- 요구를 다른 동작으로 대체했다고 적은 항목(표·해석)이 V2 이상, 또는 코드에 대체·미구현 표시가 있는데 '완료'
- (3차 QA) 근거가 import 확인뿐(E4) · Start-Process 로 띄운 것뿐(E10) · 테스트가 실제 형식이 아닌 파일을 씀(E7)
- (3차 QA) 근거 테스트가 '구현 위치(파일:함수)'의 함수를 부르지 않음, 여러 요구를 함수 한두 개만 부르는 테스트로 V2(E5)
- (3차 QA) V3 인데 구현 위치의 함수가 출력·값 저장뿐인 껍데기(E4, V2 면 알리기만) — 분석은 agent/codecheck.py

알려 주기만 하는 것 (blocks_done=False — 보고 품질)
- 의존성 이름·버전(의존성·스택 줄)이 실제 설치와 다름 · '정리'에 적은 삭제가 이번 실행 기록에 없음 · 한자 섞임
- 작업 폴더에 확장자와 다른 형식의 파일(공백·0 바이트로 채운 .mp4 등)이 있는데 보고에 없음

과제·언어를 가리지 않는 규칙만 둔다. 언어별 분석은 Python·JavaScript/TypeScript·C# 을 알고, 모르는 언어는 판단을 건너뛴다.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from agent import codecheck

EXEC_TOOLS = ("run_command", "run_python")
SKIP_DIRS = {".venv", "venv", "env", "node_modules", "bin", "obj", "__pycache__", ".git", "dist", "build",
             ".pytest_cache", ".mypy_cache", "_workflow", ".idea", ".vs", ".vscode"}
CODE_EXT = {".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".cs", ".java", ".go", ".rs", ".rb",
            ".cpp", ".c", ".h", ".hpp", ".kt", ".swift", ".php", ".vb", ".fs"}
MAX_FILES = 400

_EXIT = re.compile(r"^exit_code: (-?\d+)")
_GRADE = re.compile(r"\bV([0-4])\b")
_RESULT = re.compile(r"결과\s*[:：]\s*\**\s*(부분\s*완료|완료|실패)")
_TREE = re.compile(r"[├└]──")
_FILE = re.compile(r"(?<![\w/\\.-])((?:[\w.\-]+[/\\])*[\w\-][\w.\-]*\.(?:pyw?|m?js|cjs|tsx?|jsx|cs|csproj|sln|java|go|rs|rb|ps1|sh|bat|html|json|toml|txt|md))\b",
                   re.I)
_RUNNER = re.compile(r"\b(pytest|unittest|node\s+--test|npm\s+(?:run\s+)?test|npx\s+(?:jest|vitest|mocha)|jest|vitest|mocha|"
                     r"dotnet\s+test|go\s+test|cargo\s+test|ctest)\b", re.I)
_SPAN = re.compile(r"`([^`\n]{3,200})`")
_CMD_START = re.compile(r"^\s*(?:\.?[\w.\\/-]*(?:python3?|py|pytest|node|npm|npx|dotnet|go|cargo|java|ruby)(?:\.exe)?)\b", re.I)
_CLAIM = re.compile(r"(만들었|작성했|저장했|생성했|실행했|설치했|수정했|고쳤|통과했|확인했|돌렸|"
                    r"\b(?:created|saved|wrote|written|installed|executed|passed|verified)\b)", re.I)
_SUBST = re.compile(r"(대체|대신|흉내|시뮬레이션|가짜로|임시로 구현|미구현|구현하지\s*(?:못|않)|구현\s*안\s*함|"
                    r"\binstead\b|substitut|simulat|\bfake\b|not\s+(?:yet\s+)?implemented)", re.I)
_NOT_X_BUT_Y = re.compile(r"(?:가|이)\s*아닌\s+\S.{0,40}?(?:으로|로)\s*(?:구현|대체|처리|만들|조절|했)")
_CODE_STRONG = re.compile(r"(\bTODO\b|\bFIXME\b|\bXXX\b|미구현|구현\s*안\s*함|구현하지\s*(?:못|않)|not\s+(?:yet\s+)?implemented|"
                          r"흉내|simulat|\bfake\b)", re.I)
_CODE_WEAK = re.compile(r"(대체|대신|\binstead\b|substitut|workaround|fallback)", re.I)
_CODE_LIMIT = re.compile(r"(지원|제한|불가|못\s|안\s*됨|안\s*되|않|unsupported|not\s+supported|limited|cannot|can't)", re.I)
_HAN = re.compile(r"[㐀-䶿一-鿿]+")
_DELETE_VERB = re.compile(r"(삭제|지웠|지움|지우고|제거|\bremoved?\b|\bdeleted?\b)", re.I)
_DELETE_CMD = re.compile(r"(remove-item|\brm\s|\bri\s|\bdel\s|\berase\s|\brmdir\b|\brd\s|rmtree|unlink|os\.remove|shutil\.rmtree)", re.I)
_DEP = re.compile(r"(?<![\w.-])([A-Za-z][\w.\-]{1,60})\s*(==|>=|~=|\^|\s|@|v)\s*v?(\d+(?:\.\d+){1,3})\b")
_HEAD = re.compile(r"^\s*#{2,4}\s*(.+?)\s*$")


@dataclass
class Step:
    """실행 기록 한 줄 (도구 하나)."""
    name: str
    args: dict[str, Any]
    ok: bool
    output: str

    @property
    def text(self) -> str:
        return str(self.args.get("command") or self.args.get("code") or "")

    @property
    def exit_code(self) -> int | None:
        m = _EXIT.match(self.output or "")
        return int(m.group(1)) if m else None


@dataclass
class Finding:
    text: str
    blocks_done: bool = True


@dataclass
class Row:
    num: str
    requirement: str
    location: str
    grade: int
    evidence: str
    line: str


# ───────────────────────── 보고 읽기

def is_report(answer: str) -> bool:
    """개발 보고 양식(결과 줄·검증 표·파일 구조)을 쓴 답인지."""
    return bool(_RESULT.search(answer) or parse_rows(answer) or _TREE.search(answer))


def claims_action(answer: str) -> bool:
    """'만들었다·실행했다' 같은 한 일 주장 + 파일 이름 — 도구를 안 썼으면 지어낸 것."""
    return bool(_CLAIM.search(answer) and _FILE.search(answer))


def _col(header: list[str] | None, names: tuple[str, ...]) -> int | None:
    if not header:
        return None
    return next((i for i, h in enumerate(header) if any(n in h for n in names)), None)


def parse_rows(answer: str) -> list[Row]:
    """'요구사항별 검증' 표에서 등급(V0~V4)이 적힌 행."""
    rows: list[Row] = []
    header: list[str] | None = None
    for line in answer.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|") and s.count("|") >= 3):
            if s:
                header = header if s.startswith("|") else None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        low = [c.lower() for c in cells]
        if any("등급" in c or "grade" in c for c in low) and not _GRADE.search(s):
            header = low
            continue
        gi = _col(header, ("등급", "grade"))
        gcell = cells[gi] if gi is not None and gi < len(cells) else s
        g = _GRADE.search(gcell)
        if not g:
            continue
        get = lambda names, default: (cells[i] if (i := _col(header, names)) is not None and i < len(cells)  # noqa: E731
                                      else (cells[default] if default < len(cells) else ""))
        rows.append(Row(num=get(("#", "번호", "no"), 0), requirement=get(("요구", "requirement", "기능"), 1),
                        location=get(("구현", "위치", "location", "file"), 2), grade=int(g.group(1)),
                        evidence=get(("근거", "evidence", "확인"), 4), line=s))
    return rows


def sections(answer: str) -> dict[str, str]:
    """### 제목 → 내용 (제목은 소문자로)."""
    out: dict[str, str] = {}
    current, buf = "", []
    for line in answer.splitlines():
        m = _HEAD.match(line)
        if m:
            out[current] = out.get(current, "") + "\n".join(buf)
            current, buf = m.group(1).lower(), []
        else:
            buf.append(line)
    out[current] = out.get(current, "") + "\n".join(buf)
    return out


def _section(answer: str, *names: str) -> str:
    return "\n".join(v for k, v in sections(answer).items() if any(n in k for n in names))


def _without_code(text: str) -> str:
    return re.sub(r"`[^`\n]*`", " ", re.sub(r"```.*?```", " ", text, flags=re.S))


# ───────────────────────── 작업 폴더 읽기

def _files(folder: Path, exts: set[str] | None = None) -> list[Path]:
    out: list[Path] = []
    if not folder.is_dir():
        return out
    stack = [folder]
    while stack and len(out) < MAX_FILES:
        d = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS and not p.name.startswith("."):
                    stack.append(p)
            elif exts is None or p.suffix.lower() in exts:
                out.append(p)
    return out


def _find(workdir: Path, token: str) -> Path | None:
    """보고에 적힌 경로(상대·절대·일부·이름만)를 작업 폴더에서 찾는다. 뒤쪽 경로 조각이 겹치면 같은 파일로 본다."""
    t = token.strip().strip("`'\"").replace("\\", "/")
    p = Path(t)
    if p.is_absolute() and p.exists():
        return p
    parts = [x.lower() for x in t.split("/") if x and x != "."]
    if not parts:
        return None
    for f in _files(workdir):
        rel = [x.lower() for x in f.relative_to(workdir).parts]
        k = min(len(rel), len(parts))
        if rel[-k:] == parts[-k:]:
            return f
    return None


def _project_root(path: Path, workdir: Path) -> Path:
    """파일이 속한 프로젝트 폴더 = 작업 폴더 바로 아래 폴더 (없으면 작업 폴더)."""
    try:
        rel = path.relative_to(workdir)
    except ValueError:
        return path.parent
    return workdir / rel.parts[0] if len(rel.parts) > 1 else workdir


def _is_test_path(p: Path) -> bool:
    name = p.name.lower()
    parts = {x.lower() for x in p.parts[:-1]}
    return bool(parts & {"tests", "test", "__tests__", "spec"}) or name.startswith("test_") or \
        bool(re.search(r"(_test|\.test|\.spec|tests?)\.\w+$", name))


# ───────────────────────── 테스트가 프로젝트 코드를 거치는가

def _py_modules(project: Path) -> tuple[set[str], set[str], set[str]]:
    """(import 이름이 될 수 있는 것, 파일 이름, python -m 으로 돌릴 수 있는 패키지) — 테스트 파일은 뺀다."""
    mods: set[str] = set()
    files: set[str] = set()
    runnable: set[str] = set()
    for p in _files(project, {".py"}):
        rel = p.relative_to(project)
        if _is_test_path(rel) or p.name == "conftest.py":
            continue
        mods.update(x for x in rel.parts[:-1])   # 패키지·src 폴더 이름 (from src.player import …)
        mods.add(p.stem)
        files.add(p.name)
        if p.name == "__main__.py" and len(rel.parts) > 1:
            runnable.add(rel.parts[-2])
    return mods - {"__init__", "__main__", "setup", "tests", "test"}, files - {"__init__.py"}, runnable


def _py_exercises(source: str, project: Path) -> bool | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    mods, files, runnable = _py_modules(project)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] in mods for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                return True
            if node.module and node.module.split(".")[0] in mods:
                return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):   # 하위 프로세스로 실행: "-m pkg", "main.py"
            v = node.value.replace("\\", "/")
            if v in runnable or any(v.endswith(f) for f in files if f != "__main__.py"):
                return True
    return False


_LAUNCH = re.compile(r"\b(?:Start-Process|saps|Invoke-Item)\b|(?:^|[;&|]\s*)(?:start|ii)\s", re.I | re.M)
_JS_IMPORT = re.compile(r"""(?:require\(\s*|import\s*\(\s*|from\s+|import\s+)['"](\.{1,2}/[^'"]+)['"]""")


def _js_exercises(source: str, file: Path) -> bool:
    for m in _JS_IMPORT.finditer(source):
        target = (file.parent / m.group(1)).resolve()
        if not _is_test_path(target):
            return True
    return False


def _cs_exercises(file: Path, workdir: Path) -> bool | None:
    for d in (file.parent, *file.parents):
        projects = list(d.glob("*.csproj"))
        if projects:
            text = " ".join(p.read_text(encoding="utf-8", errors="replace") for p in projects)
            return "<ProjectReference" in text or not _is_test_path(file)
        if d == workdir:
            break
    return None


def exercises_project(file: Path, workdir: Path, source: str | None = None) -> bool | None:
    """테스트(또는 실행한 코드)가 프로젝트 자신의 코드를 거치는지. 모르는 언어면 None."""
    project = _project_root(file, workdir)
    try:
        text = source if source is not None else file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ext = file.suffix.lower()
    if ext in (".py", ".pyw"):
        return _py_exercises(text, project)
    if ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"):
        return _js_exercises(text, file)
    if ext == ".cs":
        return _cs_exercises(file, workdir)
    return None


def _cd_base(command: str, workdir: Path) -> Path:
    base = workdir
    for m in re.finditer(r"(?:^|[;&|\n]\s*)(?:cd|set-location|sl|pushd|push-location)\s+(?:-path\s+)?(\"[^\"]+\"|'[^']+'|[^\s;&|]+)",
                         command, re.I):
        target = m.group(1).strip("\"'")
        p = Path(target)
        base = p if p.is_absolute() else (base / p)
    return base


def tests_run(step: Step, workdir: Path) -> list[tuple[Path, str | None]]:
    """실행 한 번이 돌린 테스트·스크립트 파일들 (run_python 은 코드 자체)."""
    if step.name == "run_python":
        return [(workdir / "_run_python.py", step.text)]
    command = step.text
    base = _cd_base(command, workdir)
    out: list[tuple[Path, str | None]] = []
    for tok in _FILE.findall(command):
        if Path(tok).suffix.lower() in CODE_EXT:
            p = Path(tok) if Path(tok).is_absolute() else base / tok
            if p.is_file():
                out.append((p, None))
    if out:
        return out
    if re.search(r"\b(pytest|unittest)\b", command, re.I):
        return [(p, None) for p in _files(base, {".py"}) if _is_test_path(p.relative_to(base)) and p.name != "conftest.py"]
    if re.search(r"node\s+--test|npm\s+(?:run\s+)?test|jest|vitest|mocha", command, re.I):
        return [(p, None) for p in _files(base, {".js", ".mjs", ".cjs", ".ts"}) if _is_test_path(p.relative_to(base))]
    if re.search(r"dotnet\s+test", command, re.I):
        return [(p, None) for p in _files(base, {".cs"}) if _is_test_path(p.relative_to(base))]
    if re.search(r"(?:python3?|py)(?:\.exe)?\s+-m\s+[\w.]+", command, re.I):   # python -m pkg : 프로젝트 실행
        return [(base / "_command.py", f"import {re.search(r'-m\s+([\w.]+)', command).group(1)}")]
    return out


# ───────────────────────── 검사

def _refs(row: Row) -> tuple[list[str], list[str], list[str]]:
    text = row.evidence + " " + row.location
    files = [Path(t.replace("\\", "/")).name.lower() for t in _FILE.findall(row.evidence)]
    runners = [re.sub(r"\s+", " ", m.lower()) for m in _RUNNER.findall(text)]
    spans = [re.sub(r"\s+", " ", s.strip().lower()) for s in _SPAN.findall(row.evidence) if _CMD_START.match(s)]
    return files, runners, spans


def _matching(row: Row, execs: list[Step]) -> list[Step]:
    files, runners, spans = _refs(row)
    if not (files or runners or spans):
        return []
    out = []
    for st in execs:
        t = re.sub(r"\s+", " ", st.text.lower().replace("\\", "/"))
        if any(f in t for f in files) or any(r in t for r in runners) or any(s in t for s in spans):
            out.append(st)
    return out


def _test_like(step: Step) -> bool:
    """테스트 러너나 확인용 코드(run_python) — 프로그램 자체를 실행한 것이 아닌 근거."""
    return step.name == "run_python" or bool(_RUNNER.search(step.text))


def _import_check(step: Step) -> bool:
    code = step.text if step.name == "run_python" else codecheck.command_python_code(step.text)
    return bool(code) and codecheck.import_only(code)


def _launch_only(step: Step) -> bool:
    return step.name == "run_command" and bool(_LAUNCH.search(step.text)) and "[stdout]" not in (step.output or "")


def _read_sources(runs: list[tuple[Path, str | None]]) -> list[tuple[Path, str]]:
    out = []
    for p, src in runs:
        if src is None:
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        out.append((p, src))
    return out


def _project_defs(workdir: Path) -> set[str]:
    names: set[str] = set()
    for p in _files(workdir, set(codecheck.PY) | set(codecheck.BRACE)):
        if not _is_test_path(p.relative_to(workdir)):
            try:
                names |= codecheck.defined_symbols(p.read_text(encoding="utf-8", errors="replace"), p.suffix.lower())
            except OSError:
                continue
    return names


def _check_rows(rows: list[Row], execs: list[Step], workdir: Path,
                fakes: list[tuple[Path, str]] | None = None) -> list[Finding]:
    out: list[Finding] = []
    ok_execs = [s for s in execs if s.exit_code == 0]
    groups: dict[tuple[int, ...], list[tuple[Row, list[tuple[Path, str]]]]] = {}
    for r in rows:
        label = f"#{r.num} {r.requirement[:30]}".strip()
        for tok in _FILE.findall(r.location):
            if Path(tok).suffix.lower() in CODE_EXT | {".csproj", ".sln", ".html"} and _find(workdir, tok) is None:
                out.append(Finding(f"{label}: 구현 위치 '{tok}' 파일이 작업 폴더에 없습니다"))
                break
        if r.grade < 2:
            continue
        if _SUBST.search(r.requirement + " " + r.location + " " + r.evidence):
            out.append(Finding(f"{label}: 다른 동작으로 대체했다고 적었는데 V{r.grade} 입니다. 요구를 그대로 못 했으면 V0(미충족)입니다"))
            continue
        matched = _matching(r, ok_execs)
        if not matched:
            failed = _matching(r, [s for s in execs if s.exit_code not in (0, None)])
            why = "실패했습니다(종료 코드 0 이 아님)" if failed else "이번 실행에서 돌린 기록이 없습니다"
            out.append(Finding(f"{label}: V{r.grade} 근거로 적은 명령·테스트를 {why}. "
                               f"V2·V3 는 이번에 실행한 명령 + 핵심 출력이 근거입니다 (코드 설명·로직 구현은 근거가 아님)"))
            continue
        if all(_import_check(st) for st in matched):   # E4: 규격 — import 성공은 증거가 아니다
            out.append(Finding(f"{label}: V{r.grade} 근거가 import 확인뿐입니다 (규격: import 성공은 증거가 아님). "
                               "요구 기능을 실제로 부르는 테스트·실행이 근거입니다"))
            continue
        if all(_launch_only(st) for st in matched):    # E10: Start-Process 의 종료 코드 0 = 띄웠다는 뜻
            out.append(Finding(f"{label}: V{r.grade} 근거가 프로그램을 띄운 것(Start-Process)뿐입니다. 종료 코드 0 은 띄웠다는 "
                               "뜻일 뿐 동작 확인이 아닙니다 — 스스로 종료하며 관측값을 출력하는 실행이 근거입니다"))
            continue
        cited = [t for t in _FILE.findall(r.evidence) if Path(t).suffix.lower() in CODE_EXT]
        runs: list[tuple[Path, str | None]] = []
        for st in matched:
            runs += tests_run(st, workdir)
        if cited:   # 행에 적은 파일만 본다
            names = {Path(c.replace("\\", "/")).name.lower() for c in cited}
            runs = [x for x in runs if x[0].name.lower() in names] or \
                [(p, None) for c in cited if (p := _find(workdir, c)) is not None]
        verdicts = [(p, exercises_project(p, workdir, src)) for p, src in runs]
        known = [(p, v) for p, v in verdicts if v is not None]
        if known and not any(v for _, v in known):
            names = ", ".join(sorted({p.name for p, _ in known})[:3])
            out.append(Finding(f"{label}: V{r.grade} 근거({names})가 프로젝트 자신의 코드를 import·실행하지 않습니다 "
                               f"(라이브러리만 직접 시험). 이 요구를 구현한 코드를 거치는 테스트·실행이 있어야 V{r.grade} 입니다"))
            continue
        sources = _read_sources(runs)
        fake = [(f, why) for f, why in (fakes or []) if any(f.name in src for _, src in sources)]
        if fake:   # E7: 실제 형식이 아닌 입력으로 한 시험
            f, why = fake[0]
            out.append(Finding(f"{label}: V{r.grade} 근거 테스트가 쓰는 '{f.name}' 은(는) 실제 {f.suffix} 형식이 아닙니다 ({why}). "
                               "실제 형식의 입력으로 시험해야 근거입니다"))
            continue
        if not (sources and all(_test_like(st) for st in matched)):
            continue   # 프로그램 자체를 실행한 근거는 함수 대조를 하지 않는다
        syms = codecheck.location_symbols(r.location)
        if syms:   # E5: 근거 테스트가 이 요구를 구현한 함수를 부르는가
            refs = set().union(*(codecheck.referenced(src, p.suffix.lower()) for p, src in sources))
            if not any(s in refs for s in syms):
                names = ", ".join(sorted({p.name for p, _ in sources})[:2])
                out.append(Finding(f"{label}: V{r.grade} 근거 테스트({names})가 구현 위치의 {', '.join(syms[:3])} 을(를) 부르지 "
                                   "않습니다 — 이 요구를 시험하지 않았습니다. 그 함수를 부르는 테스트를 더하거나 등급을 낮추세요"))
        else:
            groups.setdefault(tuple(sorted(id(st) for st in matched)), []).append((r, sources))
    out += _check_groups(groups, workdir)
    return out


def _check_groups(groups: dict[tuple[int, ...], list[tuple[Row, list[tuple[Path, str]]]]], workdir: Path) -> list[Finding]:
    """E5: 구현 위치에 함수를 안 적은 요구 여럿을 같은 테스트로 V2 이상이라 했는데, 그 테스트가 부르는 프로젝트 함수가 훨씬 적다."""
    out: list[Finding] = []
    defs: set[str] | None = None
    for items in groups.values():
        if len(items) < 3:
            continue
        defs = _project_defs(workdir) if defs is None else defs
        refs = set().union(*(codecheck.referenced(src, p.suffix.lower()) for p, src in items[0][1]))
        used = sorted(refs & defs)
        if len(used) * 2 < len(items):
            nums = ", ".join(f"#{r.num}" for r, _ in items)
            out.append(Finding(f"요구 {len(items)}개({nums})를 같은 테스트로 V2 이상이라 했는데, 그 테스트가 부르는 프로젝트 함수·클래스는 "
                               f"{len(used)}개({', '.join(used[:4]) or '없음'})뿐입니다. 시험하지 않은 요구는 등급을 낮추거나 "
                               "그 요구의 코드를 부르는 테스트를 더하세요"))
    return out


def _check_stubs(rows: list[Row], workdir: Path) -> list[Finding]:
    """E4: 구현 위치의 함수가 출력·값 저장뿐인 껍데기. V3 이상이면 막고, V2 는 알린다."""
    out: list[Finding] = []
    seen: dict[tuple[Path, tuple[str, ...]], list[Row]] = {}
    for r in rows:
        if r.grade < 2:
            continue
        code = [t for t in _FILE.findall(r.location) if Path(t).suffix.lower() in CODE_EXT]
        f = _find(workdir, code[0]) if code else None
        if f is None:
            continue
        funcs, stubs = codecheck.stub_functions(f)
        if not funcs:
            continue
        syms = [s for s in codecheck.location_symbols(r.location) if s in funcs]
        if syms:
            bad = syms if all(s in stubs for s in syms) else []
        else:
            bad = sorted(stubs) if len(stubs) >= 2 and len(stubs) * 2 >= len(funcs) else []
        if bad:
            seen.setdefault((f, tuple(bad)), []).append(r)
    for (f, bad), rs in seen.items():
        top = max(r.grade for r in rs)
        nums = ", ".join(f"#{r.num}" for r in rs)
        out.append(Finding(f"{nums}: 구현 위치({f.name})의 {', '.join(bad[:5])} 이(가) 출력(print)·값 저장뿐이라 요구의 실제 동작이 "
                           f"없습니다 (껍데기 구현). 실제 효과를 내는 코드로 구현하고 그 효과(예: 바뀐 상태·출력값)를 관측해야 "
                           f"V{top} 입니다", top >= 3))
    return out


# ───────────────────────── E11: V1 = ① 문법 ② 정의되지 않은 이름 ③ 모듈 로드 (기준 00_공통.md, 2026-11-06 변경)

_V1_CLAIM = re.compile(r"\bV1\b[^\n|]{0,30}(통과|OK|ok|pass|성공|완료)|정적\s*검증[^\n|]{0,10}(통과|성공)")
_V1_NAMES = re.compile(r"\b(pyflakes|flake8|ruff|eslint|tsc)\b|dotnet\s+build", re.I)


def _v1_load(step: Step, workdir: Path) -> bool:
    """③ 모듈 로드: 모든 모듈 import(importlib·pkgutil) 또는 프로젝트 모듈을 실제로 import 한 실행 (dotnet build 는 셋 다)."""
    text = step.text
    if re.search(r"dotnet\s+build", text, re.I):
        return True
    code = text if step.name == "run_python" else codecheck.command_python_code(text)
    if code:
        return "importlib" in code or "pkgutil" in code or bool(_py_exercises(code, workdir))
    return bool(re.search(r"node\s+(?:--input-type=\S+\s+)?-e\b", text) and re.search(r"import\(|require\(", text))


def _check_v1(answer: str, rows: list[Row], ledger: list[Step], workdir: Path) -> list[Finding]:
    """V1 이라고 한 요구(또는 'V1 통과' 라고 쓴 답)에 ②·③ 판정 기록이 있는가. 문법 검사(compileall 등)만으로는 V1 이 아니다."""
    if not (any(r.grade == 1 for r in rows) or _V1_CLAIM.search(answer)):
        return []
    from agent import standards
    lang = standards.detect_from_folder(workdir)
    if lang is None:
        return []
    execs = [s for s in ledger if s.name in EXEC_TOOLS]
    ok = [s for s in execs if s.exit_code == 0]
    failed_load = [s for s in execs if s.exit_code not in (0, None) and _v1_load(s, workdir)]
    if failed_load:
        return [Finding("V1 ③ 모듈 로드(import)가 실패했습니다 — 그 오류를 고치기 전에는 V1 이 아닙니다: "
                        f"`{failed_load[-1].text[:80]}`")]
    missing = []
    if not any(_V1_NAMES.search(s.text) for s in ok):
        missing.append("② 정의되지 않은 이름 (예: python 은 pyflakes, node 는 eslint no-undef)")
    if not any(_v1_load(s, workdir) for s in ok):
        missing.append("③ 모듈 로드 (모든 모듈을 import)")
    if not missing:
        return []
    return [Finding("V1 은 ① 문법 ② 정의되지 않은 이름 ③ 모듈 로드가 모두 오류 0 이어야 하는데, 이번 실행에 " + " · ".join(missing) +
                    " 판정 기록이 없습니다 (compileall·node --check 는 ① 만 봅니다). 언어별 규격의 V1 판정 명령을 실행하세요")]


def fake_media(workdir: Path) -> list[tuple[Path, str]]:
    """E7: 확장자는 영상·이미지·문서인데 실제 그 형식이 아닌 파일."""
    return [(p, why) for p in _files(workdir, set(codecheck.MEDIA_EXT)) if (why := codecheck.fake_reason(p))]


def _check_fake_media(answer: str, fakes: list[tuple[Path, str]], workdir: Path) -> list[Finding]:
    out = []
    for p, why in fakes[:3]:
        if p.name not in answer:
            out.append(Finding(f"작업 폴더의 '{p.relative_to(workdir).as_posix()}' 은(는) 확장자와 달리 실제 {p.suffix} 형식이 아닙니다 "
                               f"({why}). 테스트 입력으로 썼다면 그 테스트는 근거가 아니며, 보고에 밝혀야 합니다", False))
    return out


_COMMON_WORDS = {"기능", "구현", "처리", "지원", "가능", "추가", "사용", "표시", "동작", "하기", "있게", "the", "and", "with"}


def _check_interpretation(answer: str, rows: list[Row], result: str | None) -> list[Finding]:
    """해석·한계에 '대체했다'고 적은 요구가 V2 이상이면 걸린다."""
    out: list[Finding] = []
    text = _section(answer, "해석", "한계", "확인 못", "interpret", "limitation")
    for line in text.splitlines():
        if not (_SUBST.search(line) or _NOT_X_BUT_Y.search(line)):
            continue
        low = line.lower()   # 한국어는 조사가 붙으므로('CSV가') 요구의 낱말이 줄 안에 들어 있는지로 본다
        hit = [r for r in rows if r.grade >= 2 and any(
            w in low for w in re.findall(r"[\w가-힣]{2,}", r.requirement.lower()) if w not in _COMMON_WORDS)]
        if hit or result == "완료":
            target = f"#{hit[0].num} {hit[0].requirement[:30]} 이 V{hit[0].grade}" if hit else "결과가 '완료'"
            out.append(Finding(f"보고에 '{line.strip()[:80]}' 라고 적었는데 {target} 입니다. "
                               "요구를 다른 방법·동작으로 바꿨다면 그 요구는 미충족(V0)이고, 무엇으로 대체했는지 밝혀야 합니다"))
            break
    return out


def _comment_text(line: str, ext: str) -> str:
    if ext in (".py", ".pyw", ".rb", ".sh", ".ps1"):
        i = line.find("#")
        return line[i:] if i >= 0 else ""
    for marker in ("//", "/*", "*"):
        i = line.find(marker)
        if i >= 0 and (marker != "*" or line.strip().startswith("*")):
            return line[i:]
    return ""


def _check_source_markers(workdir: Path) -> list[Finding]:
    out: list[Finding] = []
    for p in _files(workdir, CODE_EXT):
        if _is_test_path(p.relative_to(workdir)):
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            c = _comment_text(line, p.suffix.lower())
            if c and (_CODE_STRONG.search(c) or (_CODE_WEAK.search(c) and _CODE_LIMIT.search(c))):
                out.append(Finding(f"코드에 대체·미구현 표시가 있습니다: {p.relative_to(workdir).as_posix()}:{n} '{c.strip()[:70]}' — "
                                   "요구를 그대로 구현했는지 확인하고, 아니면 그 요구를 미충족으로 보고하세요"))
                if len(out) >= 2:
                    return out
                break
    return out


# ───────────────────────── 보고 품질 (알려 주기만)

def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.lower())


def installed_packages(workdir: Path, ledger: Iterable[Step]) -> dict[str, str]:
    """실제로 설치된 패키지 → 버전: 프로젝트 가상환경(pip list), node_modules, .csproj, 이번 실행의 설치 출력."""
    found: dict[str, str] = {}
    for st in ledger:
        for m in re.finditer(r"Successfully installed ([^\n]+)", st.output or ""):
            for item in m.group(1).split():
                name, _, ver = item.rpartition("-")
                if name and re.match(r"\d", ver):
                    found[_norm(name)] = ver
    for py in list(workdir.glob("*/.venv/Scripts/python.exe")) + list(workdir.glob(".venv/Scripts/python.exe")) + \
            list(workdir.glob("*/.venv/bin/python")):
        try:
            r = subprocess.run([str(py), "-m", "pip", "list", "--format=json", "--disable-pip-version-check"],
                               capture_output=True, text=True, timeout=30)
            for item in json.loads(r.stdout or "[]"):
                found[_norm(item["name"])] = item["version"]
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    for pkg in list(workdir.glob("*/node_modules/*/package.json")) + list(workdir.glob("*/node_modules/@*/*/package.json")):
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            found[_norm(data["name"])] = str(data["version"])
        except (OSError, ValueError, KeyError):
            continue
    for proj in _files(workdir, {".csproj"}):
        for m in re.finditer(r'<PackageReference\s+Include="([^"]+)"\s+Version="([^"]+)"',
                             proj.read_text(encoding="utf-8", errors="replace")):
            found[_norm(m.group(1))] = m.group(2)
    return found


def _check_dependencies(answer: str, workdir: Path, ledger: list[Step]) -> list[Finding]:
    text = _without_code(_section(answer, "의존성", "dependenc", "스택", "stack"))   # E9: 스택 줄에 적은 버전도
    pairs = [(n, v) for n, op, v in _DEP.findall(text)   # '>=6.4' 같은 최소 버전 선언은 설치 버전 주장이 아니다
             if op.strip() not in (">=", "~=", "^") and not re.fullmatch(r"(python|v|version|버전)", n, re.I)]
    if not pairs:
        return []
    real = installed_packages(workdir, ledger)
    if not real:
        return []
    out = []
    for name, ver in pairs:
        key = _norm(name)
        if key in real:
            if not (real[key] == ver or real[key].startswith(ver + ".")):
                out.append(Finding(f"보고의 의존성 '{name} {ver}' 이(가) 실제 설치({name} {real[key]})와 다릅니다", False))
        else:
            near = [k for k in real if k.startswith(key + "-")]
            if near:
                out.append(Finding(f"보고의 의존성 '{name} {ver}' 은(는) 설치돼 있지 않습니다. 실제로 설치된 것: "
                                   + ", ".join(f"{k} {real[k]}" for k in near[:2]), False))
    return out[:4]


def _check_cleanup(answer: str, ledger: list[Step]) -> list[Finding]:
    text = _section(answer, "정리", "cleanup", "clean-up")
    out: list[Finding] = []
    deletes = [s.text.lower().replace("\\", "/") for s in ledger if s.name in EXEC_TOOLS and _DELETE_CMD.search(s.text)]
    for line in text.splitlines():
        if not _DELETE_VERB.search(line) or re.search(r"(이전|지난|앞선|previous|earlier)", line, re.I):
            continue
        tokens = [t.strip("`/").lower().replace("\\", "/") for t in re.findall(r"`([^`]+)`", line)] or \
            [t.strip("/").lower() for t in re.findall(r"[\w.\-]*[_/.][\w.\-/]+", line) if len(t) > 2]
        missing = [t for t in tokens if t and not any(t.rstrip("/") in d for d in deletes)]
        if missing:
            out.append(Finding(f"'정리'에 적은 '{missing[0]}' 삭제가 이번 실행 기록에 없습니다. "
                               "이전 실행에서 한 일이면 그렇게 밝히세요", False))
    return out[:2]


def check_language(answer: str, task: str) -> list[Finding]:
    """사용자가 쓰지 않은 문자(한자)가 답에 섞였는지. 코드·인용은 뺀다."""
    if _HAN.search(task or ""):
        return []
    runs = _HAN.findall(_without_code(answer))
    if not runs:
        return []
    return [Finding(f"답에 사용자가 쓰지 않은 문자(한자)가 섞였습니다: {', '.join(dict.fromkeys(runs))[:40]} — "
                    "사용자가 쓴 언어로 고쳐 쓰세요", False)]


# ───────────────────────── 한 번에

@dataclass
class Review:
    findings: list[Finding] = field(default_factory=list)
    no_tools: bool = False     # 도구 0회인데 한 일을 주장

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocks_done]


def review(answer: str, task: str, ledger: list[Step], workdir: Path, *, used_any_tool: bool,
           dev_report: bool) -> Review:
    """최종 답을 실행 기록과 대조한다. dev_report: 개발 보고로 볼지 (규격 프로필이거나 보고 양식을 썼을 때)."""
    rv = Review()
    if not used_any_tool and (is_report(answer) or claims_action(answer)):
        rv.no_tools = True
        rv.findings.append(Finding("이번 실행에서 도구를 한 번도 쓰지 않았는데 보고에 파일·실행·검증 결과가 있습니다. "
                                   "실제로 한 일이 아닙니다"))
        rv.findings += check_language(answer, task)
        return rv
    if dev_report:
        rows = parse_rows(answer)
        m = _RESULT.search(answer)
        result = re.sub(r"\s+", " ", m.group(1)) if m else None
        execs = [s for s in ledger if s.name in EXEC_TOOLS and s.ok]
        fakes = fake_media(workdir)
        rv.findings += _check_rows(rows, execs, workdir, fakes)
        rv.findings += _check_stubs(rows, workdir)
        rv.findings += _check_v1(answer, rows, ledger, workdir)
        rv.findings += _check_interpretation(answer, rows, result)
        rv.findings += _check_fake_media(answer, fakes, workdir)
        if result == "완료" or any(r.grade >= 2 for r in rows):
            rv.findings += _check_source_markers(workdir)
        rv.findings += _check_dependencies(answer, workdir, ledger)
        rv.findings += _check_cleanup(answer, ledger)
    rv.findings += check_language(answer, task)   # D11: 개발 보고가 아니어도
    return rv
