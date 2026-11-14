"""안전 장치: 경로 샌드박스와 승인 정책.

- 파일 도구는 반드시 Sandbox.resolve() 를 거친 경로만 쓴다.
- 셸/파이썬 실행은 경로 제한을 우회할 수 있으므로 항상 사람의 승인을 받는다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.config import Settings
    from agent.profiles import Profile
    from agent.tools.base import Tool

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_DRIVE = re.compile(r"^[A-Za-z]:")
_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:(?![\\/])")  # "C:foo" = C 드라이브의 '현재 폴더' 기준 → 예측 불가
_SHORT_NAME = re.compile(r"~\d")                     # PROGRA~1 같은 8.3 짧은 이름


class SandboxError(Exception):
    pass


@dataclass(frozen=True)
class Root:
    path: Path
    writable: bool
    shared: bool = False   # 사용자가 허용한 공유 위치 (agent/locations.py — 늘 읽기 전용)


def _norm(p: Path | str) -> str:
    from agent import locations
    return locations.norm(p)   # 네트워크 경로는 realpath 없이 (연결 안 되는 서버에서 멈추지 않게)


def _is_within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


class Sandbox:
    def __init__(self, roots: list[Root], cwd: Path):
        # 더 구체적인(긴) 경로를 먼저 검사해야 rw 폴더 안의 ro 하위 폴더 같은 설정이 맞게 동작한다.
        self.roots = sorted(roots, key=lambda r: len(str(r.path)), reverse=True)
        self.cwd = cwd

    @classmethod
    def from_settings(cls, settings: Settings) -> Sandbox:
        from agent import locations
        roots = []
        for d in settings.allowed_dirs:
            p = settings.path(d.path)
            if d.mode == "rw":
                p.mkdir(parents=True, exist_ok=True)
            roots.append(Root(Path(os.path.realpath(p)), d.mode == "rw"))
        for share in locations.paths():   # 사용자가 허용한 공유 위치 — 모든 용도가 읽기 전용으로 (결정 ①·⑥)
            roots.append(Root(Path(share), False, shared=True))
        sb = cls(roots, settings.workspace_path)
        sb.settings = settings
        return sb

    settings: Settings | None = None

    def resolve(self, raw: str, write: bool = False) -> Path:
        from agent import locations
        s = locations.to_path(raw)   # file://서버/공유/… · file:///Z:/… 도 경로로
        if not s:
            raise SandboxError("경로가 비어 있습니다")
        if locations.is_device(s) or (s.startswith(("\\\\", "//")) and not locations.is_unc(s)):
            raise SandboxError("장치 경로(\\\\?\\·\\\\.\\)는 허용되지 않습니다")
        if locations.is_unc(s):
            return self._resolve_share(raw, s, write)
        if _DRIVE_RELATIVE.match(s):
            raise SandboxError("'C:foo' 형식은 허용되지 않습니다. 'C:\\foo' 처럼 쓰세요")
        body = s[2:] if _DRIVE.match(s) else s
        if ":" in body:
            raise SandboxError("경로에 ':' 를 쓸 수 없습니다 (대체 데이터 스트림 차단)")
        for part in re.split(r"[\\/]+", body):
            if part.split(".")[0].strip().upper() in _RESERVED:
                raise SandboxError(f"예약된 장치 이름입니다: {part}")
            if _SHORT_NAME.search(part):
                raise SandboxError(f"짧은 이름(8.3) 형식은 허용되지 않습니다: {part}")

        p = Path(s)
        if not p.is_absolute():
            p = self.cwd / p
        # realpath 는 심볼릭 링크와 정션을 실제 위치로 풀어준다. 이 결과로 판정해야
        # '허용 폴더 안의 정션 → 바깥 폴더' 같은 우회가 막힌다.
        real = _norm(p)
        for root in self.roots:
            if _is_within(real, _norm(root.path)):
                remote = locations._is_remote_drive(str(p))   # 매핑된 네트워크 드라이브는 realpath 가 서버에 묻는다
                return self._checked(root, raw, Path(os.path.normpath(p)) if remote else Path(os.path.realpath(p)), write)
        raise SandboxError(self._outside(raw))

    def _resolve_share(self, raw: str, s: str, write: bool) -> Path:
        """네트워크 공유 경로(\\\\서버\\공유\\…): 허용한 공유 위치 안일 때만."""
        for part in re.split(r"[\\/]+", s.lstrip("\\/")):
            if ":" in part:
                raise SandboxError("경로에 ':' 를 쓸 수 없습니다 (대체 데이터 스트림 차단)")
            if part.split(".")[0].strip().upper() in _RESERVED:
                raise SandboxError(f"예약된 장치 이름입니다: {part}")
        real = _norm(s)
        for root in self.roots:
            if root.shared and _is_within(real, _norm(root.path)):
                return self._checked(root, raw, Path(os.path.normpath(s)), write)
        raise SandboxError(self._outside(raw))

    def _checked(self, root: Root, raw: str, path: Path, write: bool) -> Path:
        if write and not root.writable:
            if root.shared:
                raise SandboxError(f"공유 위치는 읽기 전용입니다: {root.path} — 결과는 작업 폴더에 저장하세요")
            raise SandboxError(f"읽기 전용 폴더입니다: {root.path}")
        if root.shared:   # 허용한 공유 위치 안이라도 에이전트 설정·비밀·시스템 폴더는 안 된다 (정션 등으로 이어진 경우까지)
            from agent import locations
            if why := locations.refuse_reason(str(path), self.settings):
                raise SandboxError(why)
        return path

    def _outside(self, raw: str) -> str:
        allowed = ", ".join(str(r.path) for r in self.roots)
        return f"허용된 폴더 밖입니다: {raw} (허용: {allowed})"

    def describe(self) -> str:
        return "\n".join(f"  - {r.path} ({'읽기/쓰기' if r.writable else '읽기 전용'})" for r in self.roots)


def approval_reason(tool: Tool, profile: Profile, tainted: bool) -> str | None:
    """이 도구 호출에 사람의 승인이 필요하면 그 이유를, 아니면 None 을 돌려준다.

    tainted: 이번 실행에서 웹 페이지·문서처럼 외부 내용을 읽었는지. 외부 내용에 숨은 지시(프롬프트
    인젝션)가 있을 수 있으므로, 그 뒤로는 자동 승인을 끈다.
    """
    if tool.permission == "read":
        return None
    if tool.permission == "write":
        if profile.auto_approve_writes and not tainted:
            return None
        return "파일을 만들거나 수정합니다"
    if tool.permission == "send":
        return "사내 시스템의 데이터를 바꿀 수 있는 API 호출입니다 (주소와 내용을 확인하세요)"
    if tool.permission == "admin":
        return "에이전트가 접근할 수 있는 사내 범위를 넓히고 설정에 저장합니다 (주소를 확인하세요)"
    return "명령/코드를 실행합니다 (경로 제한이 적용되지 않으니 내용을 확인하세요)"


# ───────────────────────── 되돌릴 수 없는 삭제 (자동 승인 중에도 사람에게 묻는다)
# 실행 도구는 원래 승인 대상이지만 '전부 자동'이면 묻지 않는다. 그래도 작업 폴더·프로젝트 폴더를 통째로 지우는 명령은
# 되돌릴 수 없고 이전 작업(가상환경 설치 등)을 날리므로 항상 확인한다 (QA R08: Remove-Item -Recurse -Force . 로 프로젝트 삭제).

_SPLIT = re.compile(r"""((?:"[^"]*"|'[^']*'|[^;&|\n])+)""")
_TOKEN = re.compile(r"""("[^"]*"|'[^']*'|\S+)""")
_PS_DELETE = {"remove-item", "rm", "ri", "del", "erase", "rd", "rmdir"}
_CD = {"cd", "set-location", "sl", "chdir", "pushd", "push-location"}
_LISTING = {"get-childitem", "gci", "ls", "dir"}
_PROJECT_MARKS = (".venv", "venv", ".git", "node_modules", "pyproject.toml", "package.json", "requirements.txt", "src")


def _strip(token: str) -> str:
    return token.strip().strip("\"'")


def _resolve(base: Path, target: str) -> Path:
    t = _strip(target).replace("/", os.sep)
    if "*" in t or "?" in t:   # a\* · .\* · * → 그 폴더의 내용 전부
        t = t.split("*")[0].split("?")[0].rstrip("\\/") or "."
    p = Path(t)
    return Path(os.path.normpath(p if p.is_absolute() else base / p))


def _danger(path: Path, workdir: Path) -> str | None:
    work = Path(os.path.normpath(workdir))
    if path == work or work.is_relative_to(path):
        return "작업 폴더 전체"
    if not path.is_relative_to(work):
        return "작업 폴더 밖의 폴더"
    if path.parent == work and path.is_dir() and not path.name.startswith(("_", ".")):
        return "프로젝트 폴더 전체"
    if path.is_dir() and any((path / m).exists() for m in _PROJECT_MARKS) or any(path.glob("*.sln")) or any(path.glob("*.csproj")):
        return "프로젝트(소스·가상환경 포함)"
    return None


def _recursive(tokens: list[str]) -> bool:
    for t in tokens[1:]:
        low = _strip(t).lower()
        if re.fullmatch(r"-r(e(c(u(r(s(e)?)?)?)?)?)?(:\$true)?", low) or low in ("-rf", "-fr", "/s"):
            return True
    return False


def _targets(tokens: list[str]) -> list[str]:
    out, skip = [], False
    for t in tokens[1:]:
        low = _strip(t).lower()
        if skip:
            skip = False
            if low not in ("-path", "-literalpath"):
                continue
        if low in ("-path", "-literalpath", "-lp"):
            continue
        if low in ("-include", "-exclude", "-filter", "-erroraction", "-ea"):
            skip = True
            continue
        if low.startswith(("-", "$")) or re.fullmatch(r"/[a-z]", low):   # -Force · $var · cmd 의 /s /q
            continue
        out.append(t)
    return out


def destructive_reason(tool_name: str, arguments: dict, workdir: Path) -> str | None:
    """작업 폴더·프로젝트 폴더(또는 작업 폴더 밖)를 통째로 지우는 명령이면 그 이유. 아니면 None."""
    if tool_name == "run_command":
        return _shell_delete(str(arguments.get("command", "")), workdir)
    if tool_name == "run_python":
        return _python_delete(str(arguments.get("code", "")), workdir)
    return None


def _shell_delete(command: str, workdir: Path) -> str | None:
    base = Path(workdir)
    previous: list[str] = []
    for statement in _SPLIT.findall(command):
        tokens = _TOKEN.findall(statement.strip())
        if not tokens:
            continue
        head = _strip(tokens[0]).lower()
        if head in ("cmd", "cmd.exe") and len(tokens) > 2:   # cmd /c rmdir /s /q x
            tokens = tokens[2:]
            head = _strip(tokens[0]).lower()
        if head in _CD and len(tokens) > 1:
            base = _resolve(base, _targets(tokens)[0] if _targets(tokens) else tokens[1])
            previous = tokens
            continue
        if head in _PS_DELETE and _recursive(tokens):
            targets = _targets(tokens)
            if not targets and previous and _strip(previous[0]).lower() in _LISTING:   # gci x | Remove-Item -Recurse
                targets = _targets(previous) or ["."]
            for t in targets:
                path = _resolve(base, t)
                why = _danger(path, workdir)
                if why:
                    return f"되돌릴 수 없는 삭제: '{path}' ({why}). 자동 승인 중이어도 사람이 확인합니다"
        previous = tokens
    return None


def _python_delete(code: str, workdir: Path) -> str | None:
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "rmtree" and node.args):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Call) and getattr(arg.func, "id", getattr(arg.func, "attr", "")) in ("Path", "getcwd", "cwd") :
            arg = arg.args[0] if arg.args else ast.Constant(".")
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            path = _resolve(Path(workdir), arg.value)
            why = _danger(path, workdir)
            if why:
                return f"되돌릴 수 없는 삭제: '{path}' ({why}). 자동 승인 중이어도 사람이 확인합니다"
    return None
