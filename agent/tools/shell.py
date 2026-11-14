"""실행 도구: PowerShell 명령과 파이썬 코드. 항상 사람의 승인을 거친다 (permission="exec").

Windows 주의점:
- 한국어 Windows 의 콘솔 인코딩은 cp949 라서, 출력은 바이트로 받아 UTF-8 → cp949 순서로 해석한다.
- subprocess 의 timeout 은 직접 띄운 프로세스만 죽인다. 자식·손자 프로세스까지 psutil 로 정리한다.
- 명령 문자열은 -EncodedCommand(UTF-16LE base64)로 넘겨 따옴표 이스케이프 문제를 없앤다.
- 띄운 프로세스는 Job 에 넣는다. 콘솔을 강제로 닫아도 GUI 앱·긴 스크립트가 남지 않는다 (agent/proc.py).
- 타임아웃으로 멈춰도 그때까지의 출력은 돌려준다. Python 은 파이프에 연결되면 출력을 모아 두었다가 내보내서
  강제 종료 때 사라지므로 PYTHONUNBUFFERED=1 로 바로바로 내보내게 한다 (Node·.NET 콘솔은 원래 바로 내보낸다).
- Windows PowerShell 5.1 은 native 명령에 `2>&1` 을 붙이면 성공해도 exit 1 + CLIXML 을 낸다.
  `2>&1` 을 빼고 실행하고, 실제 종료 코드를 그대로 돌려주고, 그래도 나온 CLIXML 은 글자로 풀어 준다.
- run_python 과 셸의 `python` 은 같은 '사용자용 파이썬'(시스템 Python)이다. 에이전트 자신의 .venv 는 쓰지 않는다.
"""

from __future__ import annotations

import base64
import html
import os
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field

from agent.config import ROOT
from agent.proc import job_add, kill_tree  # noqa: F401  (kill_tree: 예전 import 경로 호환)
from agent.tools.base import ToolContext, ToolError, decode_bytes, tool

SHELL = shutil.which("pwsh") or "powershell"
SHELL_NAME = "PowerShell 7 (pwsh)" if SHELL != "powershell" else "Windows PowerShell 5.1"
_PRELUDE = (
    "$ProgressPreference='SilentlyContinue'; "
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8; "
    "$global:LASTEXITCODE = $null\n"
)
# 마지막 native 명령의 종료 코드를 그대로 돌려준다 (PowerShell 은 원래 성공/실패를 0/1 로만 돌려준다)
_EPILOGUE = ("\n$__ok = $?; if ($null -ne $global:LASTEXITCODE) { exit $global:LASTEXITCODE } "
             "elseif (-not $__ok) { exit 1 } else { exit 0 }")
_AGENT_VENV = os.path.normcase(str(ROOT / ".venv"))

TIMEOUT_HINT = ("⚠ {sec}초 안에 끝나지 않아 강제로 멈췄습니다. 위 출력은 멈추기 전까지 나온 것입니다.\n"
                "끝나지 않는 프로그램(GUI·서버·감시 루프)은 원래 끝나지 않으므로 타임아웃이나 exit code 는 증거가 아닙니다. "
                "정해진 시간 뒤 스스로 종료하면서 확인한 값을 출력(또는 결과 파일로 저장)하는 테스트로 검증하세요. "
                "언어별 방법은 규격의 '끝나지 않는 프로그램 검증'에 있습니다.")


# ───────────────────────── 실행 환경

def _outside_agent_venv(path: str) -> bool:
    return not os.path.normcase(os.path.abspath(path)).startswith(_AGENT_VENV)


@lru_cache(maxsize=1)
def user_python() -> str:
    """사용자 프로그램용 파이썬 = 셸에서 `python` 을 쳤을 때 잡히는 것 (에이전트 .venv 제외)."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not _outside_agent_venv(d):
            continue
        for name in ("python.exe", "python"):
            cand = Path(d) / name
            if cand.is_file() and "WindowsApps" not in str(cand):   # MS Store 바로가기는 건너뛴다
                return str(cand)
    found = shutil.which("py")
    return found or "python"


PACKAGES_DIR = ROOT / "packages"   # 오프라인 번들이 담아 오는 개발 도구 (python wheel · npm 캐시) — QA E11


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")}
    env["PATH"] = os.pathsep.join(d for d in env.get("PATH", "").split(os.pathsep) if d and _outside_agent_venv(d))
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "NO_COLOR": "1", "DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1"})
    env.update(package_env(PACKAGES_DIR))
    return env


def package_env(packages: Path) -> dict[str, str]:
    """폐쇄망에서 사용자 프로젝트가 V1·V2 판정 도구(pyflakes·pytest·eslint …)를 설치할 수 있게, 번들의 패키지 폴더를
    pip·npm 이 보게 한다. 'packages/.offline' 표시가 있으면(폐쇄망 설치) 인터넷 색인은 아예 보지 않는다."""
    env: dict[str, str] = {}
    offline = (packages / ".offline").exists()
    if (packages / "python").is_dir():
        env["PIP_FIND_LINKS"] = str(packages / "python")
        if offline:
            env["PIP_NO_INDEX"] = "1"
    if (packages / "npm-cache").is_dir():
        env["npm_config_cache"] = str(packages / "npm-cache")
        env["npm_config_prefer_offline" if not offline else "npm_config_offline"] = "true"
    return env


# ───────────────────────── PowerShell 5.1 의 2>&1 · CLIXML

_MERGE = re.compile(r"(?<![\w>&])2>&1(?=\s*(?:\||;|\)|$))", re.M)
_CLIXML = re.compile(r"#< CLIXML\s*(<Objs.*?</Objs>)", re.S)
_CLIXML_S = re.compile(r'<S S="(?:Error|Warning|Verbose|Debug|Information)">(.*?)</S>', re.S)
_PS_NOISE = re.compile(r"^\s*(At line:\d+|위치 줄:\d+|\+ |\+$|\s*\+ (CategoryInfo|FullyQualifiedErrorId)|"
                       r"\s*\+\s*~+|.*NativeCommandError)", re.M)


def strip_merge_redirect(command: str) -> tuple[str, bool]:
    if SHELL != "powershell":   # pwsh 7 은 이 문제가 없다
        return command, False
    new = _MERGE.sub("", command)
    return new, new != command


# ───────────────────────── PowerShell 5.1 에 없는 문법 (QA E8: '&&' 실패로 스텝 낭비)
# 소형 모델은 '&&'·'mkdir -p'·'rm -rf' 를 자주 쓴다. 5.1 은 파싱 단계에서 거부해 아무것도 실행하지 않으므로,
# 같은 뜻의 5.1 문법으로 바꿔 실행하고 무엇을 바꿨는지 알려 준다 (따옴표·괄호 안은 건드리지 않는다).

LAUNCH_HINT = ("ℹ Start-Process 는 프로그램을 띄우기만 합니다. 종료 코드 0 은 띄웠다는 뜻일 뿐 프로그램이 제대로 동작했다는 근거가 "
               "아닙니다 — 동작은 스스로 종료하며 관측값을 출력하는 실행으로 확인하세요.")
_LAUNCH = re.compile(r"\b(?:Start-Process|saps)\b", re.I)


def _split_top(command: str, sep: str) -> list[str] | None:
    """따옴표·괄호 밖의 sep 로 나눈다. here-string(@' '@)이 있으면 None (손대지 않음)."""
    if "@'" in command or '@"' in command:
        return None
    parts, buf, quote, depth, i = [], [], "", 0, 0
    while i < len(command):
        c = command[i]
        if quote:
            if c == "`" and quote == '"' and i + 1 < len(command):
                buf += [c, command[i + 1]]
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
        elif depth == 0 and command.startswith(sep, i):
            parts.append("".join(buf))
            buf = []
            i += len(sep)
            continue
        buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _chain(parts: list[str], cond: str) -> str:
    out = parts[-1].strip()
    for p in reversed(parts[1:-1]):
        out = f"{p.strip()}; if ({cond}) {{ {out} }}"
    return f"{parts[0].strip()}; if ({cond}) {{ {out} }}"


def _tokens(text: str) -> list[str]:
    return re.findall(r"\"[^\"]*\"|'[^']*'|\S+", text)


def _q(token: str) -> str:
    """PowerShell 글자 그대로 따옴표 ('…')."""
    return token if token[:1] in ("'", '"') else "'" + token.replace("'", "''") + "'"


def _statement(st: str) -> str:
    """bash 식 명령 하나를 5.1 문법으로 (모르는 모양이면 그대로)."""
    lead = st[:len(st) - len(st.lstrip())]
    m = re.match(r"(mkdir|md)\s+(.+)$", st.strip(), re.S)
    if m:
        toks = _tokens(m.group(2))
        paths = [t for t in toks if t not in ("-p", "--parents")]
        if paths and all(not t.startswith("-") for t in paths) and (len(paths) > 1 or len(paths) < len(toks)):
            return f"{lead}New-Item -ItemType Directory -Force -Path {', '.join(map(_q, paths))} | Out-Null"
    m = re.match(r"rm\s+(.+)$", st.strip(), re.S)
    if m:
        toks = _tokens(m.group(1))
        flags = [t for t in toks if re.fullmatch(r"-[rRf]+|--recursive|--force", t)]
        paths = [t for t in toks if t not in flags]
        if flags and paths and all(not t.startswith("-") for t in paths):
            fl = "".join(flags).lower()
            opts = (" -Recurse" if "r" in fl else "") + (" -Force" if "f" in fl else "")
            return f"{lead}Remove-Item{opts} -Path {', '.join(map(_q, paths))}"
    m = re.match(r"ls\s+-[laAh]+\b(.*)$", st.strip(), re.S)
    if m:
        return f"{lead}Get-ChildItem -Force{m.group(1)}"
    m = re.match(r"which\s+([\w.\-]+)\s*$", st.strip())
    if m:
        return f"{lead}(Get-Command {m.group(1)}).Source"
    m = re.match(r"export\s+([A-Za-z_]\w*)=(.*)$", st.strip(), re.S)
    if m:
        return f"{lead}$env:{m.group(1)} = {_q(m.group(2).strip())}"
    return st


def ps51_compat(command: str) -> tuple[str, str]:
    """(실행할 명령, 바꿨으면 알림 글자)."""
    if SHELL != "powershell":
        return command, ""
    fixed = command
    ands, ors = _split_top(fixed, "&&"), _split_top(fixed, "||")
    if ands and ors and not (len(ands) > 1 and len(ors) > 1):   # 섞여 있으면 뜻이 애매해 건드리지 않는다
        if len(ands) > 1 and all(p.strip() for p in ands):
            fixed = _chain(ands, "$?")
        elif len(ors) > 1 and all(p.strip() for p in ors):
            fixed = _chain(ors, "-not $?")
    statements = _split_top(fixed, ";")
    if statements:
        fixed = ";".join(_statement(s) for s in statements)
    if fixed == command:
        return command, ""
    shown = fixed if len(fixed) <= 240 else fixed[:240] + " …"
    return fixed, f"(Windows PowerShell 5.1 에 없는 문법이라 바꿔 실행했습니다: {shown})"


def clean_clixml(text: str) -> str:
    """'#< CLIXML <Objs>…' 를 사람이 읽는 오류 글자로 바꾼다."""
    def repl(m: re.Match) -> str:
        parts = [html.unescape(s) for s in _CLIXML_S.findall(m.group(1))]
        joined = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda x: chr(int(x.group(1), 16)), "".join(parts))
        lines = [ln.rstrip() for ln in joined.replace("\r\n", "\n").split("\n")]
        return "\n".join(ln for ln in lines if ln.strip() and not _PS_NOISE.match(ln))
    return _CLIXML.sub(repl, text)


# ───────────────────────── 설치·다운로드 정책 (폐쇄망·프로젝트 로컬 설치 규격)

# pip 설치: exe = pip 앞에 붙은 실행 파일 경로 ('.venv\Scripts\python -m pip', '.venv\Scripts\pip', 'python -m pip', 'pip')
_PIP = re.compile(r"(?P<exe>[^\s;&|]*?)(?:\s+(?:-\S+\s+)*-m\s+)?\bpip3?(?:\.exe)?[\"']?\s+install\b(?P<rest>[^;|\n]*)", re.I)


def _is_venv_pip(match: re.Match, command: str) -> bool:
    exe = match.group("exe").lower()
    return "venv" in exe or bool(re.search(r"\\activate|/activate|activate\.ps1", command, re.I))
_GLOBAL_INSTALLS = [
    (re.compile(r"\bnpm(?:\.cmd)?\s+(?:i|install|add)\b[^;|\n]*\s(-g|--global)\b", re.I), "npm 전역 설치(-g)"),
    (re.compile(r"\bdotnet\s+tool\s+install\b[^;|\n]*\s(-g|--global)\b", re.I), "dotnet 전역 도구 설치(-g)"),
    (re.compile(r"\b(winget|choco|scoop)(?:\.exe)?\s+install\b", re.I), "시스템 패키지 관리자 설치"),
]
_DOWNLOADS = re.compile(r"\b(Invoke-WebRequest|iwr|Invoke-RestMethod|irm|curl(?:\.exe)?|wget|Start-BitsTransfer|"
                        r"DownloadFile|DownloadString|git\s+clone)\b", re.I)


# 프로젝트 로컬 설치 명령 → 그 전에 돌려야 하는 '환경 확인' (규격 1장 2단계)
_LOCAL_INSTALLS = [
    ("python", _PIP),
    ("node-web", re.compile(r"\bnpm(?:\.cmd)?\s+(?:i|install|add)\s+(?!-)[^\s;|]+", re.I)),   # 패키지 이름이 있는 설치
    ("dotnet", re.compile(r"\bdotnet\s+add\s+\S+\s+package\b|\bdotnet\s+add\s+package\b", re.I)),
]
_ENV_CHECKS = {
    "python": re.compile(r"pip3?(\.exe)?\s+(list|show|freeze)\b|-m\s+pip\s+(list|show|freeze)\b|"
                         r"\b(python3?|py)(\.exe)?\s+(--version|-V)\b|import\s+\w+|find_spec", re.I),
    "node-web": re.compile(r"\bnpm(\.cmd)?\s+(ls|list|view|info|outdated)\b|\bnode(\.exe)?\s+(--version|-v)\b", re.I),
    "dotnet": re.compile(r"\bdotnet\s+(--list-sdks|--info|--version|list\b.*\bpackage|new\s+list|package\s+search)", re.I),
}
ENV_HINT = {
    "python": "python --version; python -m pip list  (또는 python -c \"import <모듈>\")",
    "node-web": "node --version; npm ls --depth=0",
    "dotnet": "dotnet --list-sdks; dotnet list package",
}


def note_env_checks(ctx: ToolContext, text: str) -> None:
    """이 실행에서 '이미 있는 것'을 확인한 언어를 기억한다 (설치 전 환경 확인 규칙용)."""
    checked = ctx.memo.setdefault("env_checked", set())
    for lang, rx in _ENV_CHECKS.items():
        if rx.search(text):
            checked.add(lang)


def check_install_policy(ctx: ToolContext, command: str) -> None:
    """프로젝트 밖(시스템 전역) 설치, 인터넷 다운로드, 환경 확인 없는 설치를 막는다. 규격 1장 2단계·3장."""
    policy = ctx.settings.policy
    if policy.block_global_install:
        for m in _PIP.finditer(command):
            if "--user" in m.group("rest"):
                raise ToolError("`pip install --user` 는 사용자 전역 설치라 막혀 있습니다. 프로젝트 .venv 에 설치하세요.")
            if not _is_venv_pip(m, command):
                raise ToolError("시스템 파이썬에 바로 설치하는 명령은 막혀 있습니다 (규격: 설치는 프로젝트 로컬만). "
                                "프로젝트 폴더에서 `python -m venv .venv` 로 가상환경을 만든 뒤 "
                                "`.venv\\Scripts\\python -m pip install <패키지>` 로 설치하고, requirements.txt 등에 버전과 함께 적으세요. "
                                "먼저 `python -m pip list` 로 이미 있는지 확인하세요.")
        for rx, what in _GLOBAL_INSTALLS:
            if rx.search(command):
                raise ToolError(f"{what}는 막혀 있습니다 (규격: 전역 설치 금지, 프로젝트 로컬만). "
                                "프로젝트 안에 설치하는 방법(npm install <pkg>, dotnet add package, 로컬 도구 매니페스트)을 쓰세요.")
    if policy.block_downloads and _DOWNLOADS.search(command):
        raise ToolError("인터넷에서 파일을 내려받는 명령은 막혀 있습니다 (규격: 폐쇄망 전제). "
                        "이미 이 PC 에 있는 도구·라이브러리로 해결하세요. 외부 실행 파일이나 큰 파일이 꼭 필요하면 "
                        "만들기 전에 사용자에게 무엇을 왜 받아야 하는지 물어보세요.")
    if policy.require_env_check:
        checked = ctx.memo.get("env_checked", set())
        for lang, rx in _LOCAL_INSTALLS:
            if rx.search(command) and lang not in checked:
                raise ToolError("설치하기 전에 이미 있는 것부터 확인하세요 (규격 1장 2단계 '환경 확인'). "
                                f"먼저 따로 실행: `{ENV_HINT[lang]}` — 결과를 보고 정말 없을 때만 프로젝트에 설치합니다.")


# ───────────────────────── 실행

def run_process(ctx: ToolContext, argv: list[str], timeout_sec: int, note: str = "") -> str:
    timeout_sec = max(1, min(timeout_sec, ctx.settings.limits.command_timeout))
    proc = subprocess.Popen(
        argv,
        cwd=ctx.sandbox.cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    job_add(proc.pid)   # 관리 서버·CLI 가 어떻게 끝나든(콘솔 강제 종료 포함) 이 명령과 그 자식들도 같이 끝난다
    deadline = time.monotonic() + timeout_sec
    stopped = ""
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if ctx.cancel.is_set():
                stopped = "사용자가 중지함"
            elif time.monotonic() > deadline:
                stopped = f"{timeout_sec}초 시간 초과로 중지함"
            else:
                continue
            kill_tree(proc.pid)
            try:   # 멈추기 전까지 나온 출력은 파이프에 남아 있다 → 마저 읽는다
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = b"", b""
            break

    parts = [f"exit_code: {proc.returncode}" + (f" ({stopped})" if stopped else "")]
    if note:
        parts.append(note)
    if out:
        parts.append("[stdout]\n" + clean_clixml(decode_bytes(out)).rstrip())
    if err:
        parts.append("[stderr]\n" + clean_clixml(decode_bytes(err)).rstrip())
    if not out and not err and stopped:
        parts.append("(멈추기 전까지 출력이 없었습니다)")
    if stopped.endswith("시간 초과로 중지함"):
        parts.append(TIMEOUT_HINT.format(sec=timeout_sec))
    return "\n".join(parts)


@tool("exec")
def run_command(
    ctx: ToolContext,
    command: Annotated[str, Field(description="PowerShell 명령. 여러 명령은 ; 로 구분 (&& 금지). stderr 는 따로 모으니 2>&1 을 붙이지 않는다")],
    timeout_sec: Annotated[int, Field(description="최대 실행 시간(초). 끝나지 않는 프로그램은 스스로 종료하는 테스트로 확인한다")] = 60,
) -> str:
    """작업 폴더에서 PowerShell 명령을 실행하고 실제 종료 코드와 stdout·stderr 를 돌려준다. 여기서 `python` 은 run_python 과 같은 사용자용 파이썬이다."""
    check_install_policy(ctx, command)
    note_env_checks(ctx, command)
    command, stripped = strip_merge_redirect(command)
    command, compat = ps51_compat(command)
    notes = ["(이 셸에서 2>&1 은 성공해도 오류처럼 보이게 만들어 빼고 실행했습니다. stderr 는 아래에 따로 보여 줍니다)" if stripped else "",
             compat, LAUNCH_HINT if _LAUNCH.search(command) else ""]
    note = "\n".join(n for n in notes if n)
    encoded = base64.b64encode((_PRELUDE + command + _EPILOGUE).encode("utf-16-le")).decode("ascii")
    argv = [SHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    return run_process(ctx, argv, timeout_sec, note)


@tool("exec")
def run_python(
    ctx: ToolContext,
    code: Annotated[str, Field(description="실행할 파이썬 코드. 결과는 print 로 출력")],
    timeout_sec: Annotated[int, Field(description="최대 실행 시간(초)")] = 60,
) -> str:
    """파이썬 코드를 작업 폴더에서 바로 실행한다 (계산·데이터 확인용). 셸의 `python` 과 같은 시스템 Python 이다. 프로젝트 실행·테스트는 run_command 로 프로젝트 .venv 의 파이썬을 쓴다."""
    note_env_checks(ctx, code)   # 'import x' 로 있는지 본 것도 환경 확인이다
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    script = ctx.run_dir / f"script_{int(time.time() * 1000)}.py"
    script.write_text(code, encoding="utf-8")
    # -u: 출력 바로 내보내기(타임아웃 때도 남게), -X utf8: 입출력 UTF-8
    return run_process(ctx, [user_python(), "-X", "utf8", "-u", str(script)], timeout_sec)


# ───────────────────────── 실패 종류 (A6: 같은 종류의 실패가 이어지는지 보기)

_EXIT = re.compile(r"^exit_code: (-?\d+)(.*)$", re.M)
# 같은 '원인'으로 묶는 기준. 빌드 오류는 코드가 매번 달라도(CS0246 → CS1061) 같은 종류로 본다.
_SIGNATURES = [
    (re.compile(r"\berror (?:CS|MC|NETSDK|MSB|BC|FS)\d+|\bTS\d{4}:", re.I), "빌드 실패"),
    (re.compile(r"npm ERR!|npm error"), "npm 실패"),
    (re.compile(r"^(?:[\w$]+\.)*(\w+(?:Error|Exception))\b(?=:|\s*$)", re.M), "예외 {0}"),   # Python·JS·.NET 예외 이름
]


def failure_kind(tool_name: str, ok: bool, output: str) -> str | None:
    """실행 결과가 실패면 그 '종류'를 한 줄로 (예: '시간 초과', '빌드 실패', '예외 TypeError'). 성공이면 None."""
    if tool_name not in ("run_command", "run_python"):
        return None
    m = _EXIT.search(output)
    if m and "시간 초과" in m.group(2):
        return "시간 초과"
    if not m:
        return None if ok else "실행 거부·오류"
    if m.group(1) == "0":
        return None
    for rx, label in _SIGNATURES:
        found = rx.search(output)
        if found:
            return label.format(*found.groups()) if found.groups() else label
    return f"종료 코드 {m.group(1)}"
