"""설치된 Claude Code CLI 찾기 · 버전 · 인증 상태.

찾는 순서: 설정에 적은 경로 → CCGUI_CLAUDE_PATH → PATH 의 claude.exe → 네이티브 설치(~/.local/bin)
→ VS Code 계열 확장에 들어 있는 claude.exe(가장 높은 버전) → SDK 에 들어 있는 CLI(마지막 수단).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 부모 Claude Code 세션(예: VS Code 확장 안에서 서버를 띄웠을 때)과 엮이게 만드는 환경변수.
# 자식 CLI 가 이것을 물려받으면 자기를 다른 세션의 하위 세션으로 착각하거나 부모의 메시지 소켓에 붙는다.
_PARENT_SESSION_ENV = {
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EMIT_STARTUP_TIMING",
    "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT", "CLAUDE_CODE_ENABLE_TASKS", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_CODE_SSE_PORT",
}


def scrub_parent_session_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """부모 Claude 세션 연결용 환경변수를 지운 사본 (env 가 None 이면 os.environ 을 직접 정리)."""
    if env is None:
        for k in list(os.environ):
            if k in _PARENT_SESSION_ENV:
                os.environ.pop(k, None)
        return dict(os.environ)
    return {k: v for k, v in env.items() if k not in _PARENT_SESSION_ENV}


_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class CliInfo:
    found: bool
    path: str | None = None
    source: str | None = None
    version: str | None = None
    error: str | None = None
    candidates: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _version_key(p: Path) -> tuple[int, ...]:
    m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", str(p))
    return tuple(int(x) for x in m.groups()) if m else (0,)


def _is_native_exe(p: str) -> bool:
    return sys.platform != "win32" or p.lower().endswith(".exe")


def candidates(override: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def add(path: str | Path | None, source: str) -> None:
        if not path:
            return
        s = str(path)
        if os.path.isfile(s) and _is_native_exe(s) and all(os.path.normcase(s) != os.path.normcase(x) for x, _ in out):
            out.append((s, source))

    add(override, "설정")
    add(os.environ.get("CCGUI_CLAUDE_PATH"), "환경변수 CCGUI_CLAUDE_PATH")
    add(shutil.which("claude.exe") if sys.platform == "win32" else shutil.which("claude"), "PATH")
    home = Path.home()
    add(home / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude"), "네이티브 설치")
    exe = "claude.exe" if sys.platform == "win32" else "claude"
    for ide in (".vscode", ".vscode-insiders", ".cursor", ".windsurf"):
        hits = sorted((home / ide / "extensions").glob(f"anthropic.claude-code-*/resources/native-binary/{exe}"),
                      key=_version_key, reverse=True)
        for h in hits[:1]:
            add(h, f"{ide.strip('.')} 확장")
    try:
        import claude_agent_sdk

        add(Path(claude_agent_sdk.__file__).parent / "_bundled" / exe, "Agent SDK 내장")
    except Exception:  # noqa: BLE001
        pass
    return out


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=scrub_parent_session_env(dict(os.environ)),
        creationflags=_NO_WINDOW,
        stdin=subprocess.DEVNULL,
    )


class CliLocator:
    """CLI 탐색 결과와 인증 상태를 잠시 캐시한다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._info: CliInfo | None = None
        self._info_key: tuple[str, float] | None = None
        self._auth: dict[str, Any] | None = None
        self._auth_at = 0.0

    def locate(self, override: str = "", refresh: bool = False) -> CliInfo:
        with self._lock:
            cands = candidates(override)
            if override and not any(src == "설정" for _, src in cands):
                err = f"설정한 CLI 경로를 찾을 수 없습니다: {override}"
            else:
                err = None
            if not cands:
                self._info = CliInfo(found=False, error=err or "Claude Code CLI 를 찾지 못했습니다. "
                                     "https://claude.com/claude-code 에서 설치하세요.")
                return self._info
            path, source = cands[0]
            key = (path, os.path.getmtime(path))
            if not refresh and self._info and self._info_key == key:
                return self._info
            version, verr = None, None
            try:
                r = _run([path, "--version"], timeout=30)
                m = re.search(r"(\d+\.\d+\.\d+)", r.stdout or "")
                version = m.group(1) if m else None
                if r.returncode != 0:
                    verr = (r.stderr or r.stdout or "").strip()[:300] or f"exit {r.returncode}"
            except Exception as e:  # noqa: BLE001
                verr = f"{type(e).__name__}: {e}"
            self._info = CliInfo(
                found=version is not None,
                path=path,
                source=source,
                version=version,
                error=err or verr,
                candidates=[{"path": p, "source": s} for p, s in cands],
            )
            self._info_key = key
            return self._info

    def auth_status(self, cli_path: str | None, refresh: bool = False, ttl: float = 120.0) -> dict[str, Any]:
        if not cli_path:
            return {"loggedIn": False, "error": "CLI 없음"}
        with self._lock:
            if not refresh and self._auth is not None and time.time() - self._auth_at < ttl:
                return self._auth
        try:
            r = _run([cli_path, "auth", "status"], timeout=30)
            data = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
            auth = {
                "loggedIn": bool(data.get("loggedIn")),
                "authMethod": data.get("authMethod"),
                "apiProvider": data.get("apiProvider"),
                "email": data.get("email"),
                "orgName": data.get("orgName"),
                "subscriptionType": data.get("subscriptionType"),
            }
            if not data:
                auth["error"] = (r.stderr or r.stdout or "").strip()[:300] or "인증 상태를 읽지 못했습니다"
        except Exception as e:  # noqa: BLE001
            auth = {"loggedIn": False, "error": f"{type(e).__name__}: {e}"}
        with self._lock:
            self._auth, self._auth_at = auth, time.time()
        return auth
