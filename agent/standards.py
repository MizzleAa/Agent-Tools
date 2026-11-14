"""개발 규격 (config/standards/). 원본은 QA 의 `Agent_QA/기준/` 이고, 여기 있는 것은 복사본이다.

16k 컨텍스트를 아끼려고 이렇게 나눠 쓴다.
- 공통 규격은 요약본(프롬프트 블록 'dev_standard', agent/prompts.py)을 개발 프로필의 시스템 프롬프트에 넣는다.
- 언어별 규격(python / dotnet / node-web)은 에이전트가 그 언어를 쓰기 시작하면(파일 확장자·명령으로 감지)
  그 언어 것 하나만 시스템 프롬프트에 붙인다. read_standard 도구로 직접 읽을 수도 있다.
- 실행이 끝날 때, 산출물 폴더와 최종 보고를 규격에 비춰 점검하고 빠진 게 있으면 한 번 되돌려 보낸다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent.config import CONFIG_DIR

STANDARDS_DIR = CONFIG_DIR / "standards"
FILES = {"common": "00_공통.md", "python": "python.md", "dotnet": "dotnet.md", "node-web": "node-web.md"}
LANG_LABEL = {"python": "Python", "dotnet": "C# / .NET", "node-web": "JavaScript / Node"}
CODE_EXT = {
    "python": (".py", ".pyw"),
    "dotnet": (".cs", ".csproj", ".sln", ".xaml", ".fs", ".vb"),
    "node-web": (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html"),
}
DEV_FILES = {"pyproject.toml": "python", "requirements.txt": "python", "package.json": "node-web",
             "tsconfig.json": "node-web"}
_CMD = [
    ("dotnet", re.compile(r"\bdotnet\s+(new|build|test|run|add|sln|restore)\b", re.I)),
    ("node-web", re.compile(r"\b(npm|npx|node)(\.cmd|\.exe)?\s+\S", re.I)),
    ("python", re.compile(r"\b(python3?|py|pytest|pip3?)(\.exe)?\s+(-m\s+(venv|pip|pytest|compileall)|\S+\.py\b)|\.venv[\\/]", re.I)),
]
_SKIP_DIRS = {".venv", "venv", "node_modules", "bin", "obj", "__pycache__", ".git", "dist", "_scratch", ".pytest_cache"}


def load(name: str) -> str:
    return (STANDARDS_DIR / FILES[name]).read_text(encoding="utf-8")


def detect_from_call(tool_name: str, args: dict[str, Any]) -> str | None:
    """도구 호출 하나로 어떤 언어로 개발 중인지 알아본다 (모르면 None)."""
    if tool_name == "write_file":
        path = str(args.get("path", "")).lower()
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        if name in DEV_FILES:
            return DEV_FILES[name]
        for lang, exts in CODE_EXT.items():
            if path.endswith(exts):
                return lang
    if tool_name == "run_command":
        cmd = str(args.get("command", ""))
        for lang, rx in _CMD:
            if rx.search(cmd):
                return lang
    return None


def _code_files(folder: Path):
    for p in folder.rglob("*"):
        if p.is_file() and not (_SKIP_DIRS & set(p.relative_to(folder).parts)):
            yield p


def detect_from_folder(folder: Path) -> str | None:
    """이미 만들어진 산출물(이어서 하는 대화)로 언어를 알아본다. 가장 확실한 표시부터 본다."""
    if not folder.is_dir():
        return None
    files = list(_code_files(folder))
    names = {p.name.lower() for p in files}
    if any(n.endswith((".sln", ".csproj")) for n in names):
        return "dotnet"
    if "package.json" in names:
        return "node-web"
    if {"pyproject.toml", "requirements.txt"} & names:
        return "python"
    counts = {lang: sum(p.suffix.lower() in exts for p in files) for lang, exts in CODE_EXT.items()}
    best = max(counts, key=counts.get)
    return best if counts[best] else None


# ───────────────────────── 실행이 끝날 때 점검

def _project_dirs(workdir: Path) -> list[Path]:
    return [p for p in workdir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))]


def check_project(workdir: Path, lang: str | None) -> list[str]:
    """산출물이 규격(공통 2장 + 언어별 '프로젝트 구성')을 만족하는지 본다. 빠진 것 목록을 돌려준다."""
    from agent.session import LOG_NAME

    problems: list[str] = []
    projects = _project_dirs(workdir)
    stray = [p.name for p in workdir.iterdir() if p.is_file() and p.name != LOG_NAME]
    if not projects:
        return ["작업 폴더 안에 프로젝트 폴더(<이름>/)가 없습니다. 산출물을 프로젝트 폴더 하나에 모으세요"]
    if len(projects) > 1:
        problems.append(f"프로젝트 폴더가 여러 개입니다 ({', '.join(p.name for p in projects)}). 하나로 모으세요")
    if stray:
        problems.append(f"프로젝트 폴더 밖에 파일이 있습니다: {', '.join(stray[:8])}. 프로젝트 안(일회용이면 _scratch/)으로 옮기거나 지우세요")
    project = max(projects, key=lambda p: sum(1 for _ in _code_files(p)))
    names = {p.name.lower() for p in _code_files(project)}
    rel = [p.relative_to(project).as_posix().lower() for p in _code_files(project)]
    if "readme.md" not in names:
        problems.append(f"{project.name}/README.md 가 없습니다 (무엇인지·요구사항·실행 방법·의존성·한계)")
    if (project / "_scratch").is_dir() and any((project / "_scratch").iterdir()):
        problems.append(f"{project.name}/_scratch/ 에 임시 파일이 남아 있습니다. 지우거나 보고에 남긴 이유를 적으세요")
    if lang == "python":
        if not {"pyproject.toml", "requirements.txt"} & names:
            problems.append("의존성 선언 파일(requirements.txt 또는 pyproject.toml)이 없습니다")
        if not any(re.search(r"(^|/)tests?/(.+/)?test_[^/]+\.py$|(^|/)test_[^/]+\.py$", r) for r in rel):
            problems.append("자동 테스트(tests/test_*.py)가 없습니다")
        req = project / "requirements.txt"
        declared = req.exists() and any(ln.strip() and not ln.startswith("#") for ln in req.read_text(encoding="utf-8", errors="replace").splitlines())
        if declared and not (project / ".venv").is_dir():
            problems.append("requirements.txt 에 패키지가 있는데 프로젝트 가상환경(.venv)이 없습니다 (설치는 프로젝트 .venv 에)")
    elif lang == "dotnet":
        if not any(n.endswith(".sln") for n in names):
            problems.append("솔루션 파일(.sln)이 없습니다")
        test_projects = [p for p in _code_files(project) if p.suffix.lower() == ".csproj"
                         and re.search(r"xunit|nunit|mstest", p.read_text(encoding="utf-8", errors="replace"), re.I)]
        if not test_projects:
            problems.append("테스트 프로젝트(xUnit 등 tests/<이름>.Tests)가 없습니다")
    elif lang == "node-web":
        if "package.json" not in names:
            problems.append("package.json 이 없습니다 (런타임 의존성이 없어도 테스트·스크립트용으로 둔다)")
        if not any(re.search(r"\.test\.(m?js|cjs|ts)$", r) for r in rel):
            problems.append("자동 테스트(tests/*.test.js, node --test)가 없습니다")
    if lang and ".gitignore" not in names:
        problems.append(".gitignore 가 없습니다")
    return problems


_RESULT = re.compile(r"결과\s*[:：]\s*\**\s*(부분\s*완료|완료|실패)", re.I)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_GRADE = re.compile(r"\bV([0-4])\b")


def report_result(answer: str) -> str | None:
    m = _RESULT.search(answer)
    return re.sub(r"\s+", " ", m.group(1)) if m else None


def check_report(answer: str) -> list[str]:
    """최종 보고가 공통 규격 5장(완료 정의)·6장(보고 형식)을 따르는지 본다."""
    problems: list[str] = []
    result = report_result(answer)
    if result is None:
        problems.append("첫 줄에 '## 결과: 완료 | 부분 완료 | 실패' 가 없습니다")
    rows = [r for r in _TABLE_ROW.findall(answer) if _GRADE.search(r)]
    if not rows:
        problems.append("'요구사항별 검증' 표(| # | 요구 | 구현 위치 | 등급 | 근거 |)가 없거나 등급(V0~V4)이 적혀 있지 않습니다")
    grades = [int(g) for r in rows for g in _GRADE.findall(r)[:1]]
    if result == "완료" and any(g < 3 for g in grades):
        problems.append("V3 에 못 미치는 요구(V0~V2)가 있는데 결과를 '완료'로 적었습니다. '부분 완료'로 고치거나 V3 검증을 더 하세요")
    return problems
