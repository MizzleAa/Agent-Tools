"""이 대화에서 '없는 이름'이라 실패했던 API 이름을 기억해, 다시 써 넣으면 바로 알린다 (QA E6).

R14 에서 모델이 실제 API 를 조회해 없는 이름(ImportError: cannot import name 'X')을 고쳤는데, R15(이어서 하기)에서
파일을 통째로 다시 쓰며 같은 이름을 되살렸다. 이어서 하기는 이전 실행의 도구 기록을 넘기지 않으므로,
이전 실행들의 실행 출력에서 이런 오류를 모아 이번 실행에 넘긴다 (seed).

'없는 이름' 오류만 모은다 (설치로 고치는 ModuleNotFoundError, 나중에 정의하면 되는 NameError 는 뺀다).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

EXEC_TOOLS = ("run_command", "run_python")
CODE_EXT = {".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".cs", ".java", ".kt", ".go", ".rs", ".rb", ".vue"}

# (패턴, 종류) — 종류: import = 그 이름을 가져오기, static = 형식·모듈의 멤버, member = 객체의 멤버
_PATTERNS = [
    (re.compile(r"ImportError: cannot import name '(?P<name>\w+)' from '(?P<owner>[\w.]+)'"), "import"),
    (re.compile(r"AttributeError: (?:type object|module) '(?P<owner>[\w.]+)' has no attribute '(?P<name>\w+)'"), "static"),
    (re.compile(r"AttributeError: '(?P<owner>[\w.]+)' object has no attribute '(?P<name>\w+)'"), "member"),
    (re.compile(r"does not provide an export named '(?P<name>\w+)'"), "import"),                       # JS (ESM)
    (re.compile(r"TypeError: (?P<owner>[\w.$]+)\.(?P<name>\w+) is not a (?:function|constructor)"), "member"),   # JS
    (re.compile(r"error CS0117: '(?P<owner>[\w.]+)' does not contain a definition for '(?P<name>\w+)'"), "static"),  # C#
    (re.compile(r"error CS1061: '(?P<owner>[\w.<>]+)' does not contain a definition for '(?P<name>\w+)'"), "member"),
    (re.compile(r"error CS0234: The type or namespace name '(?P<name>\w+)' does not exist in the namespace '(?P<owner>[\w.]+)'"), "import"),
]


@dataclass
class BadName:
    name: str
    owner: str
    kind: str
    line: str

    @property
    def short_owner(self) -> str:
        return self.owner.split(".")[-1] if self.owner else ""


def _distinctive(name: str) -> bool:
    """'state'·'play' 처럼 흔한 이름은 다른 객체의 멀쩡한 멤버일 수 있다 → 길고 대소문자가 섞인 이름만 단독으로 본다."""
    return len(name) >= 8 and (any(c.isupper() for c in name[1:]) or "_" in name.strip("_"))


class KnownErrors:
    def __init__(self) -> None:
        self.names: dict[tuple[str, str], BadName] = {}

    def learn(self, output: str) -> None:
        for pattern, kind in _PATTERNS:
            for m in pattern.finditer(output or ""):
                g = m.groupdict()
                line = output[max(0, m.start()):m.end()].strip()
                self.names.setdefault((g.get("owner") or "", g["name"]), BadName(g["name"], g.get("owner") or "", kind, line))

    def seed(self, events: Iterable[dict[str, Any]]) -> None:
        """이전 실행(같은 대화)의 이벤트에서 배운다."""
        for e in events:
            if e.get("type") == "ToolResult" and e.get("name") in EXEC_TOOLS:
                self.learn(e.get("output") or "")

    def check(self, path: Path, content: str, workdir: Path | None) -> list[BadName]:
        """코드 파일에 다시 들어간 '없는 이름'. 그 이름을 이 프로젝트가 직접 정의하면(고친 것) 뺀다."""
        if path.suffix.lower() not in CODE_EXT or not self.names:
            return []
        hits = []
        for bad in self.names.values():
            word = re.compile(rf"(?<![\w$]){re.escape(bad.name)}(?![\w$])")
            if not word.search(content) or re.search(rf"\b(?:def|class|function|interface|struct|enum)\s+{re.escape(bad.name)}\b|"
                                                     rf"\b{re.escape(bad.name)}\s*=", content):
                continue
            if bad.kind == "import":
                ok = True
            elif bad.short_owner and re.search(rf"{re.escape(bad.short_owner)}\s*\.\s*{re.escape(bad.name)}\b", content):
                ok = True
            else:
                ok = _distinctive(bad.name) and re.search(rf"\.\s*{re.escape(bad.name)}\b", content) is not None
            if ok and not _defined_in_project(bad, workdir):
                hits.append(bad)
        return hits


def _defined_in_project(bad: BadName, workdir: Path | None) -> bool:
    """오류가 이 프로젝트 자신의 모듈 이야기였다면(그 뒤에 정의를 더했을 수 있음) 알리지 않는다."""
    if workdir is None or not bad.owner:
        return False
    stems = {bad.owner.split(".")[0], bad.short_owner}
    for _, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in _SKIP and not d.startswith(".")]
        if any(Path(f).suffix.lower() in CODE_EXT and Path(f).stem in stems for f in files):
            return True
    return False


_SKIP = {"venv", "env", "node_modules", "bin", "obj", "__pycache__", "dist", "build"}
