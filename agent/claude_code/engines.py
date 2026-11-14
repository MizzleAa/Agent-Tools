"""CLI 엔진: Claude Code 말고 OpenAI Codex · Google Gemini CLI 도 같은 대화 화면 · 같은 지침 · 같은 승인 패널로 쓴다.

- Codex: `codex app-server` (줄 단위 JSON, "jsonrpc" 칸 없음). 지침(설정 · 프롬프트 + 하네스)은 thread/start 의 developerInstructions.
  승인: item/commandExecution/requestApproval · item/fileChange/requestApproval → 화면 승인 패널.
  approvalPolicy 는 늘 "untrusted" — Windows 에서는 Codex 샌드박스가 강제되지 않아 승인 창이 유일한 안전장치다.
- Gemini: `gemini --acp` (Agent Client Protocol, JSON-RPC 2.0). 시스템 지침을 넣는 칸이 없어 대화 첫 요청 앞에 지침을 붙인다
  (Gemini 도 자기 문맥(<session_context>)을 같은 방식으로 넣는다). 승인: session/request_permission → 화면 승인 패널.
  '항상 허용'은 고르지 않는다 (Gemini 는 그것을 세션 전체 자동 편집으로 바꾼다).
- 설치는 이 PC 에 한 번: Agent/runtime/<엔진>-cli 에 npm 으로 (전역 설치·설정을 바꾸지 않음). Codex 는 데스크톱 앱이 깔아 둔 것도 찾는다.
  로그인도 한 번 — 그 CLI 의 홈 폴더(~/.codex, ~/.gemini)에 남아 계속 쓴다.
- 화면 기록은 우리 DB(engine_messages)에 남기고, CLI 쪽 기록은 재개할 때 모델 문맥을 되살리는 데만 쓴다.

드라이버는 manager.py 의 SessionManager 가 띄우고, 이벤트는 Sink 로 넘겨 Claude Code 와 같은 모양(transcript.py)으로 화면에 보낸다.
"""
from __future__ import annotations

import asyncio
import glob
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from agent import config

log = logging.getLogger(__name__)

ENGINES = ("claude", "codex", "gemini")
LABELS = {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini"}
RUNTIME = config.ROOT / "runtime"
NPM_PACKAGES = {"codex": "@openai/codex", "gemini": "@google/gemini-cli"}
CLIENT_INFO = {"name": "agent-server", "title": "Agent", "version": "1.0"}
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_LIMIT = 64 * 1024 * 1024   # 기록 재개 · 이미지 · diff 로 한 줄이 길 수 있다


class EngineError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class RpcError(Exception):
    def __init__(self, err: dict[str, Any]):
        self.code = err.get("code")
        self.message = str(err.get("message") or err)
        self.data = err.get("data")
        super().__init__(f"{self.code}: {self.message}")


# ───────────────────────── 줄 단위 JSON-RPC 프로세스

Handler = Callable[[str, dict[str, Any]], Awaitable[Any] | Any]


class JsonRpcProcess:
    """자식 프로세스와 줄 단위 JSON 으로 주고받는다. 알림은 순서대로 따로 처리하고(읽기 루프에서 기다리면 교착),
    서버가 보낸 요청(승인 등)은 각각 따로 처리해 답한다."""

    def __init__(self, argv: list[str], *, cwd: Path | str | None = None, env: dict[str, str] | None = None,
                 jsonrpc: bool = True, on_notification: Handler | None = None, on_request: Handler | None = None,
                 on_exit: Callable[[BaseException | None], None] | None = None):
        self.argv, self.cwd, self.env, self.jsonrpc = argv, cwd, env, jsonrpc
        self.on_notification, self.on_request, self.on_exit = on_notification, on_request, on_exit
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: deque[str] = deque(maxlen=60)
        self._ids = itertools.count(1)
        self._pending: dict[Any, asyncio.Future] = {}
        self._notes: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self.closed = asyncio.Event()

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode if self.proc else None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=str(self.cwd) if self.cwd else None, env=self.env, limit=_LIMIT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=_NO_WINDOW)
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._stderr()),
                       asyncio.create_task(self._dispatch())]

    async def _stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while line := await self.proc.stderr.readline():
            self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        err: BaseException | None = None
        try:
            while line := await self.proc.stdout.readline():
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("{"):
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if "method" not in msg and "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RpcError(msg["error"] or {}))
                        else:
                            fut.set_result(msg.get("result"))
                elif "method" in msg and "id" in msg:
                    asyncio.create_task(self._answer(msg))
                elif "method" in msg:
                    self._notes.put_nowait((msg["method"], msg.get("params") or {}))
        except Exception as e:  # noqa: BLE001
            err = e
        finally:
            self._notes.put_nowait(None)
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("CLI 프로세스가 끝났습니다"))
            self._pending.clear()
            if self.proc:
                try:
                    await asyncio.wait_for(self.proc.wait(), 5)
                except asyncio.TimeoutError:
                    pass
            self.closed.set()
            if self.on_exit:
                try:
                    self.on_exit(err)
                except Exception:  # noqa: BLE001
                    log.exception("on_exit failed")

    async def _dispatch(self) -> None:
        while (item := await self._notes.get()) is not None:
            if self.on_notification:
                try:
                    r = self.on_notification(*item)
                    if asyncio.iscoroutine(r):
                        await r
                except Exception:  # noqa: BLE001
                    log.exception("notification %s failed", item[0])

    async def _answer(self, msg: dict[str, Any]) -> None:
        rid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        try:
            if self.on_request is None:
                raise RpcError({"code": -32601, "message": f"지원하지 않는 요청: {method}"})
            result = self.on_request(method, params)
            if asyncio.iscoroutine(result):
                result = await result
            await self._send({"id": rid, "result": result})
        except RpcError as e:
            await self._send({"id": rid, "error": {"code": e.code or -32603, "message": e.message}})
        except Exception as e:  # noqa: BLE001 - 서버가 답을 기다리며 멈추지 않게 오류로라도 답한다
            await self._send({"id": rid, "error": {"code": -32603, "message": str(e)}})

    async def _send(self, obj: dict[str, Any]) -> None:
        if self.jsonrpc:
            obj = {"jsonrpc": "2.0", **obj}
        if not self.proc or not self.proc.stdin or self.proc.stdin.is_closing():
            raise ConnectionError("CLI 프로세스가 끝났습니다")
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method: str, params: Any = None, timeout: float | None = 120) -> Any:
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict[str, Any] = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._send(msg)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Any = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def close(self, timeout: float = 8) -> None:
        """stdin 을 닫아 스스로 끝나게 하고, 안 끝나면 프로세스 트리째 끝낸다."""
        if not self.proc:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.close()
            await asyncio.wait_for(self.proc.wait(), timeout)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            from .procs import kill_tree
            if self.proc.returncode is None:
                await asyncio.to_thread(kill_tree, self.proc.pid)
        for t in self._tasks:
            if not t.done():
                try:
                    await asyncio.wait_for(asyncio.shield(t), 3)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    t.cancel()


# ───────────────────────── 찾기 · 설치

def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    from .cli import scrub_parent_session_env
    env = scrub_parent_session_env(dict(os.environ))
    env.update(extra or {})
    return env


def node_path() -> str | None:
    return shutil.which("node")


def _runtime_js(engine: str) -> Path | None:
    base = RUNTIME / f"{engine}-cli" / "node_modules"
    for rel in ({"codex": ["@openai/codex/bin/codex.js"],
                 "gemini": ["@google/gemini-cli/bundle/gemini.js", "@google/gemini-cli/dist/index.js"]}[engine]):
        if (p := base / rel).is_file():
            return p
    return None


def _pkg_version(js: Path) -> str | None:
    for parent in js.parents:
        f = parent / "package.json"
        if f.is_file():
            try:
                return str(json.loads(f.read_text(encoding="utf-8")).get("version") or "") or None
            except (OSError, ValueError):
                return None
    return None


@dataclass
class EngineCli:
    engine: str
    found: bool
    argv: list[str] = field(default_factory=list)   # 실행할 앞부분 (node + js 이거나 exe 하나)
    path: str | None = None
    source: str | None = None
    version: str | None = None
    error: str | None = None


def locate(engine: str, override: str = "") -> EngineCli:
    """찾는 순서 — Codex: 설정 경로 → 데스크톱 앱(%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe, 최신) → runtime/codex-cli → PATH.
    Gemini: 설정 경로 → runtime/gemini-cli → PATH."""
    node = node_path()

    def from_path(p: str, source: str) -> EngineCli:
        if p.lower().endswith((".js", ".mjs")):
            if not node:
                return EngineCli(engine, False, path=p, source=source, error="Node.js 가 없습니다 (node 명령을 찾지 못함)")
            return EngineCli(engine, True, [node, p], p, source, _pkg_version(Path(p)))
        return EngineCli(engine, True, [p], p, source)

    if override.strip():
        p = override.strip()
        if Path(p).is_file():
            return from_path(p, "설정")
        return EngineCli(engine, False, path=p, source="설정", error=f"설정한 경로가 없습니다: {p}")
    if engine == "codex":
        exes = glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe")) if sys.platform == "win32" else []
        if exes:
            return from_path(max(exes, key=os.path.getmtime), "Codex 앱")
    if (js := _runtime_js(engine)) is not None:
        return from_path(str(js), "이 PC 설치 (runtime)")
    if found := shutil.which(engine):
        return from_path(found, "PATH")
    return EngineCli(engine, False, error=f"{LABELS[engine]} CLI 가 없습니다 — CLI 탭의 '설치'로 이 PC 에 설치하세요.")


def version_of(cli: EngineCli) -> str | None:
    if cli.version or not cli.found:
        return cli.version
    try:
        r = subprocess.run([*cli.argv, "--version"], capture_output=True, text=True, timeout=20, encoding="utf-8",
                           errors="replace", creationflags=_NO_WINDOW, env=_env())
        m = re.search(r"(\d+\.\d+\.\d+[\w.-]*)", r.stdout or "")
        cli.version = m.group(1) if m else None
    except Exception as e:  # noqa: BLE001
        cli.error = f"버전을 읽지 못했습니다: {e}"
    return cli.version


class Installer:
    """runtime/<엔진>-cli 에 npm 으로 설치 (한 번 — 전역을 바꾸지 않는다). 상태: idle · running · done · failed."""

    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}

    def status(self, engine: str) -> dict[str, Any]:
        return self.state.get(engine, {"state": "idle"})

    async def install(self, engine: str) -> dict[str, Any]:
        if self.status(engine).get("state") == "running":
            return self.status(engine)
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            raise EngineError("npm_missing", "npm(Node.js)이 없어 설치할 수 없습니다. Node.js 를 먼저 설치하세요.", 409)
        prefix = RUNTIME / f"{engine}-cli"
        prefix.mkdir(parents=True, exist_ok=True)
        self.state[engine] = {"state": "running", "started": time.time(), "log": ""}

        async def run() -> None:
            try:
                p = await asyncio.create_subprocess_exec(
                    npm, "install", "--prefix", str(prefix), "--no-fund", "--no-audit", f"{NPM_PACKAGES[engine]}@latest",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_env(), creationflags=_NO_WINDOW)
                out, _ = await asyncio.wait_for(p.communicate(), 900)
                text = out.decode("utf-8", "replace")[-3000:]
                ok = p.returncode == 0 and _runtime_js(engine) is not None
                self.state[engine] = {"state": "done" if ok else "failed", "log": text,
                                      "error": None if ok else f"npm 설치 실패 (exit {p.returncode})"}
            except Exception as e:  # noqa: BLE001
                self.state[engine] = {"state": "failed", "error": f"{type(e).__name__}: {e}", "log": ""}
            status_cache.clear()

        asyncio.create_task(run())
        return self.status(engine)


installer = Installer()


# ───────────────────────── 로그인 상태 · 로그인

def gemini_home() -> Path:
    return Path(os.environ.get("GEMINI_CLI_HOME") or Path.home()) / ".gemini"


def gemini_auth() -> dict[str, Any]:
    """파일로 판단 (프로세스를 띄우지 않는다): 구글 로그인 자격 증명 또는 API 키 환경변수."""
    home = gemini_home()
    try:
        selected = (json.loads((home / "settings.json").read_text(encoding="utf-8")).get("security") or {}).get("auth", {}).get("selectedType")
    except (OSError, ValueError, AttributeError):
        selected = None
    account = None
    try:
        account = json.loads((home / "google_accounts.json").read_text(encoding="utf-8")).get("active")
    except (OSError, ValueError, AttributeError):
        pass
    if (home / "oauth_creds.json").is_file():
        return {"logged_in": True, "method": "Google 로그인", "account": account, "selected": selected}
    if os.environ.get("GEMINI_API_KEY"):
        return {"logged_in": True, "method": "API 키 (GEMINI_API_KEY)", "account": None, "selected": selected}
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
        return {"logged_in": True, "method": "Vertex AI", "account": None, "selected": selected}
    return {"logged_in": False, "method": None, "account": None, "selected": selected,
            "stale": selected == "oauth-personal"}   # 로그인했다고 적혀 있는데 자격 증명이 없음 → 시작하면 브라우저 로그인을 기다린다


async def codex_probe(cli: EngineCli) -> dict[str, Any]:
    """app-server 를 잠깐 띄워 로그인 상태와 모델 목록을 읽는다 (1초쯤)."""
    rpc = JsonRpcProcess([*cli.argv, "app-server"], env=_env(), jsonrpc=False)
    try:
        await rpc.start()
        await rpc.request("initialize", {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": False}}, 30)
        await rpc.notify("initialized")
        acc = await rpc.request("account/read", {}, 30) or {}
        try:
            models = (await rpc.request("model/list", {}, 30) or {}).get("data") or []
        except Exception:  # noqa: BLE001
            models = []
        a = acc.get("account") or {}
        return {"logged_in": bool(a),   # 토큰이 만료돼 갱신하지 못하면 account 가 비어 온다 → 다시 로그인
                "method": {"chatgpt": "ChatGPT 로그인", "apiKey": "API 키"}.get(a.get("type"), a.get("type")),
                "account": a.get("email") or a.get("workspaceName"),
                "models": [{"id": m.get("id") or m.get("model"), "label": m.get("displayName") or m.get("id"),
                            "default": bool(m.get("isDefault")),
                            "efforts": [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or [] if isinstance(e, dict)]}
                           for m in models if isinstance(m, dict) and (m.get("id") or m.get("model"))]}
    except Exception as e:  # noqa: BLE001
        return {"logged_in": False, "error": f"상태를 읽지 못했습니다: {e}", "models": []}
    finally:
        await rpc.close(5)


class StatusCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def clear(self, engine: str | None = None) -> None:
        if engine:
            self._data.pop(engine, None)
        else:
            self._data.clear()

    async def get(self, engine: str, override: str = "", refresh: bool = False, ttl: float = 120) -> dict[str, Any]:
        lock = self._locks.setdefault(engine, asyncio.Lock())
        async with lock:
            hit = self._data.get(engine)
            if hit and not refresh and time.time() - hit[0] < ttl:
                return hit[1]
            cli = await asyncio.to_thread(locate, engine, override)
            out: dict[str, Any] = {"engine": engine, "label": LABELS[engine], "installed": cli.found, "path": cli.path,
                                   "source": cli.source, "error": cli.error, "version": None, "logged_in": False,
                                   "login_method": None, "account": None, "models": []}
            if cli.found:
                out["version"] = await asyncio.to_thread(version_of, cli)
                if engine == "codex":
                    probe = await codex_probe(cli)
                    out.update(logged_in=probe.get("logged_in", False), login_method=probe.get("method"),
                               account=probe.get("account"), models=probe.get("models") or [])
                    out["error"] = out["error"] or probe.get("error")
                else:
                    auth = gemini_auth()
                    out.update(logged_in=auth["logged_in"], login_method=auth["method"], account=auth["account"])
            self._data[engine] = (time.time(), out)
            return out


status_cache = StatusCache()

_GEMINI_URL_HOOK = Path(__file__).with_name("gemini_login_url.mjs")


class LoginFlows:
    """브라우저 로그인 — 주소를 받아 화면이 연다. 끝나면(성공·실패·10분) 프로세스를 닫는다. 이 서버 PC 의 브라우저에서 해야 한다
    (로그인 응답이 127.0.0.1 로 돌아온다)."""

    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, JsonRpcProcess] = {}

    def status(self, engine: str) -> dict[str, Any]:
        return self.state.get(engine, {"state": "idle"})

    async def _finish(self, engine: str, state: dict[str, Any]) -> None:
        self.state[engine] = {**self.state.get(engine, {}), **state, "finished": time.time()}
        if rpc := self._procs.pop(engine, None):
            await rpc.close(3)
        status_cache.clear(engine)

    async def start(self, engine: str, override: str = "") -> dict[str, Any]:
        cli = await asyncio.to_thread(locate, engine, override)
        if not cli.found:
            raise EngineError("not_installed", cli.error or f"{LABELS[engine]} CLI 가 없습니다", 409)
        if old := self._procs.pop(engine, None):
            await old.close(3)
        return await (self._codex(cli) if engine == "codex" else self._gemini(cli))

    async def _codex(self, cli: EngineCli) -> dict[str, Any]:
        done = asyncio.get_running_loop().create_future()

        def on_note(method: str, params: dict[str, Any]) -> None:
            if method == "account/login/completed" and not done.done():
                done.set_result(params)

        rpc = JsonRpcProcess([*cli.argv, "app-server"], env=_env(), jsonrpc=False, on_notification=on_note)
        await rpc.start()
        self._procs["codex"] = rpc
        try:
            await rpc.request("initialize", {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": False}}, 30)
            await rpc.notify("initialized")
            r = await rpc.request("account/login/start", {"type": "chatgpt"}, 60) or {}
        except Exception as e:  # noqa: BLE001
            await self._finish("codex", {"state": "failed", "error": str(e)})
            raise EngineError("login_failed", f"Codex 로그인을 시작하지 못했습니다: {e}") from e
        url = r.get("authUrl")
        self.state["codex"] = {"state": "waiting", "url": url, "started": time.time(), "error": None}

        async def wait() -> None:
            try:
                res = await asyncio.wait_for(done, 600)
                ok = bool(res.get("success"))
                await self._finish("codex", {"state": "done" if ok else "failed", "error": None if ok else res.get("error") or "로그인 실패"})
            except asyncio.TimeoutError:
                await self._finish("codex", {"state": "failed", "error": "10분 안에 로그인을 마치지 않았습니다"})

        asyncio.create_task(wait())
        return self.status("codex")

    async def _gemini(self, cli: EngineCli) -> dict[str, Any]:
        url_file = RUNTIME / "gemini-cli" / f"login-url-{uuid.uuid4().hex[:8]}.txt"
        url_file.parent.mkdir(parents=True, exist_ok=True)
        argv = list(cli.argv)
        if argv and Path(argv[0]).name.lower().startswith("node") and _GEMINI_URL_HOOK.is_file():
            argv = [argv[0], "--import", _GEMINI_URL_HOOK.as_uri(), *argv[1:]]   # 브라우저를 직접 열지 않고 주소를 파일로 넘긴다
        rpc = JsonRpcProcess([*argv, "--acp"], env=_env({"GEMINI_CLI_NO_RELAUNCH": "true", "AGENT_LOGIN_URL_FILE": str(url_file)}))
        await rpc.start()
        self._procs["gemini"] = rpc
        try:
            await rpc.request("initialize", {"protocolVersion": 1, "clientInfo": CLIENT_INFO,
                                             "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, 60)
        except Exception as e:  # noqa: BLE001
            await self._finish("gemini", {"state": "failed", "error": str(e)})
            raise EngineError("login_failed", f"Gemini CLI 를 시작하지 못했습니다: {e}") from e
        task = asyncio.create_task(rpc.request("authenticate", {"methodId": "oauth-personal"}, 330))
        url = None
        for _ in range(60):   # 주소가 나올 때까지 (최대 15초)
            if task.done():
                break
            if url_file.is_file() and (url := url_file.read_text(encoding="utf-8").strip()):
                break
            await asyncio.sleep(0.25)
        url_file.unlink(missing_ok=True)
        if task.done() and task.exception() is None:   # 이미 로그인돼 있었음
            await self._finish("gemini", {"state": "done", "url": None})
            return self.status("gemini")
        self.state["gemini"] = {"state": "waiting", "url": url, "started": time.time(), "error": None}

        async def wait() -> None:
            try:
                await task
                await self._finish("gemini", {"state": "done", "error": None})
            except Exception as e:  # noqa: BLE001
                await self._finish("gemini", {"state": "failed", "error": getattr(e, "message", None) or str(e)})

        asyncio.create_task(wait())
        return self.status("gemini")


logins = LoginFlows()


# ───────────────────────── 드라이버 공통

class Sink(Protocol):
    def block_start(self, key: str, btype: str) -> None: ...
    def delta(self, key: str, text: str) -> None: ...
    def drop_partial(self, key: str) -> None: ...
    def message(self, norm: dict[str, Any], replaces_partial: str | None = None) -> None: ...
    def status(self, text: str | None) -> None: ...
    def notice(self, level: str, text: str) -> None: ...
    async def ask(self, tool_name: str, tool_use_id: str, tool_input: dict[str, Any], category: str) -> str: ...
    def completed(self, result: dict[str, Any]) -> None: ...
    def exited(self, err: BaseException | None) -> None: ...
    def remember_models(self, models: list[dict[str, Any]]) -> None: ...


def assistant(blocks: list[dict[str, Any]], model: str | None = None) -> dict[str, Any]:
    mid = uuid.uuid4().hex
    return {"uuid": mid, "role": "assistant", "kind": "assistant", "blocks": blocks, "model": model,
            "message_id": mid, "parent_tool_use_id": None}


def results(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"uuid": uuid.uuid4().hex, "role": "user", "kind": "tool_results", "blocks": blocks, "model": None,
            "message_id": None, "parent_tool_use_id": None}


def tool_use(tid: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": tool_input}


def tool_result(tid: str, content: str, is_error: bool = False) -> dict[str, Any]:
    from .transcript import _truncate
    text, cut = _truncate(content or "")
    return {"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": text, "truncated": cut}


def usage_out(input_tokens: int | None, output_tokens: int | None, cached: int | None = None) -> dict[str, Any]:
    return {"input_tokens": input_tokens or 0, "output_tokens": output_tokens or 0, "cache_read_input_tokens": cached or 0}


class Driver:
    engine = ""

    def __init__(self, sink: Sink, cli: EngineCli, cwd: Path):
        self.sink, self.cli, self.cwd = sink, cli, cwd
        self.rpc: JsonRpcProcess | None = None
        self.model: str | None = None
        self.effort: str | None = None
        self.mode = "default"
        self.started_at = 0.0

    @property
    def pid(self) -> int | None:
        return self.rpc.pid if self.rpc else None

    @property
    def returncode(self) -> int | None:
        return self.rpc.returncode if self.rpc else None

    @property
    def stderr_tail(self) -> list[str]:
        return list(self.rpc.stderr_tail) if self.rpc else []

    async def stop(self) -> None:
        if self.rpc:
            await self.rpc.close()

    def _fail_start(self, what: str, e: BaseException) -> EngineError:
        tail = " / ".join(self.stderr_tail[-3:])
        msg = getattr(e, "message", None) or str(e) or type(e).__name__
        return EngineError("cli_start_failed", f"{LABELS[self.engine]} {what}: {msg}" + (f" ({tail[:300]})" if tail else ""))


# ───────────────────────── Codex

class CodexDriver(Driver):
    engine = "codex"

    def __init__(self, sink: Sink, cli: EngineCli, cwd: Path):
        super().__init__(sink, cli, cwd)
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.changes: dict[str, list[dict[str, Any]]] = {}
        self.usage: dict[str, Any] | None = None
        self.error: str | None = None
        self.retry_noted = False

    def _policy(self) -> dict[str, Any]:
        return {"approvalPolicy": "untrusted", "approvalsReviewer": "user", "sandbox": "workspace-write"}

    async def start(self, *, resume: str | None, instructions: str, model: str | None, effort: str | None, mode: str,
                    dirs: list[str]) -> str:
        self.model, self.effort, self.mode = model or None, effort or None, mode
        self.rpc = JsonRpcProcess([*self.cli.argv, "app-server"], cwd=self.cwd, env=_env(), jsonrpc=False,
                                  on_notification=self._on_note, on_request=self._on_request, on_exit=self.sink.exited)
        try:
            await self.rpc.start()
            await self.rpc.request("initialize", {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": False}}, 60)
            await self.rpc.notify("initialized")
            acc = await self.rpc.request("account/read", {}, 30) or {}
        except Exception as e:  # noqa: BLE001
            await self.rpc.close(3)
            raise self._fail_start("app-server 를 시작하지 못했습니다", e) from e
        if not acc.get("account"):
            await self.rpc.close(3)
            raise EngineError("not_logged_in", "Codex 로그인이 필요합니다 — 관리 › 외부 AI 모델 › CLI 의 Codex '로그인'을 누르세요.", 409)
        cfg: dict[str, Any] = {"model_reasoning_summary": "auto"}
        if self.effort:
            cfg["model_reasoning_effort"] = self.effort
        params = {**self._policy(), "cwd": str(self.cwd), "developerInstructions": instructions or None, "config": cfg,
                  **({"model": self.model} if self.model else {})}
        r = None
        if resume:
            try:
                r = await self.rpc.request("thread/resume", {**params, "threadId": resume, "excludeTurns": True}, 120)
            except RpcError as e:
                self.sink.notice("warning", f"이전 Codex 스레드를 이어받지 못해 새로 시작합니다 (화면 기록은 그대로): {e.message}")
        try:
            if r is None:
                r = await self.rpc.request("thread/start", params, 120)
        except Exception as e:  # noqa: BLE001
            await self.rpc.close(3)
            raise self._fail_start("대화를 시작하지 못했습니다", e) from e
        self.thread_id = ((r or {}).get("thread") or {}).get("id")
        if not self.thread_id:
            await self.rpc.close(3)
            raise EngineError("cli_start_failed", "Codex 가 대화 id 를 주지 않았습니다")
        self.model = self.model or (r or {}).get("model")
        return self.thread_id

    async def send(self, text: str, images: list[dict[str, Any]], first: bool) -> None:
        assert self.rpc and self.thread_id
        self.usage, self.error, self.retry_noted, self.started_at = None, None, False, time.time()
        items: list[dict[str, Any]] = [{"type": "text", "text": text, "text_elements": []}] if text else []
        items += [{"type": "localImage", "path": im["path"]} for im in images]
        params: dict[str, Any] = {"threadId": self.thread_id, "input": items}
        if self.model:
            params["model"] = self.model
        if self.effort:
            params["effort"] = self.effort
        r = await self.rpc.request("turn/start", params, 60)
        self.turn_id = ((r or {}).get("turn") or {}).get("id")

    async def interrupt(self) -> None:
        if self.rpc and self.thread_id and self.turn_id:
            await self.rpc.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, 10)

    async def set_mode(self, mode: str) -> None:
        self.mode = mode   # 승인 처리에서 쓴다 (manager 가 acceptEdits · plan 을 판단)

    async def set_model(self, model: str | None) -> None:
        self.model = model or None   # 다음 turn/start 부터

    # ---- 알림
    def _on_note(self, method: str, p: dict[str, Any]) -> None:
        s = self.sink
        if method == "item/started":
            item = p.get("item") or {}
            t, iid = item.get("type"), str(item.get("id") or uuid.uuid4().hex)
            if t == "agentMessage":
                s.block_start(iid, "text")
            elif t == "reasoning":
                s.block_start(iid, "thinking")
            elif t == "commandExecution":
                s.message(assistant([tool_use(iid, "Bash", {"command": _codex_command(item.get("command")),
                                                            "description": item.get("cwd") or ""})], self.model))
            elif t == "fileChange":
                changes = item.get("changes") or []
                self.changes[iid] = changes
                name, inp = _codex_patch(changes)
                s.message(assistant([tool_use(iid, name, inp)], self.model))
            elif t in ("mcpToolCall", "webSearch", "dynamicToolCall", "imageGeneration"):
                inp = {k: v for k, v in item.items() if k not in ("id", "type", "status", "result", "error")}
                s.message(assistant([tool_use(iid, {"webSearch": "WebSearch"}.get(t, item.get("tool") or t), inp)], self.model))
        elif method in ("item/agentMessage/delta", "item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            if p.get("delta"):
                s.delta(str(p.get("itemId")), p["delta"])
        elif method == "item/completed":
            item = p.get("item") or {}
            t, iid = item.get("type"), str(item.get("id") or "")
            if t == "agentMessage":
                s.message(assistant([{"type": "text", "text": item.get("text") or ""}], self.model), replaces_partial=iid)
            elif t == "reasoning":
                text = "\n\n".join(x for x in (item.get("summary") or []) + (item.get("content") or []) if isinstance(x, str))
                if text.strip():
                    s.message(assistant([{"type": "thinking", "text": text}], self.model), replaces_partial=iid)
                else:
                    s.drop_partial(iid)
            elif t == "commandExecution":
                status, code = item.get("status"), item.get("exitCode")
                out = item.get("aggregatedOutput") or ""
                if status == "declined":
                    out = (out + "\n" if out else "") + "(사용자가 거부해 실행하지 않음)"
                s.message(results([tool_result(iid, out, status != "completed" or (code not in (0, None)))]))
            elif t == "fileChange":
                status = item.get("status")
                s.message(results([tool_result(iid, {"completed": "적용함", "declined": "사용자가 거부해 적용하지 않음"}.get(status, str(status)),
                                               status != "completed")]))
            elif t in ("mcpToolCall", "webSearch", "dynamicToolCall", "imageGeneration"):
                res = item.get("result") or item.get("error") or item.get("query") or ""
                s.message(results([tool_result(iid, res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)[:20000],
                                               bool(item.get("error")))]))
        elif method == "thread/tokenUsage/updated":
            self.usage = ((p.get("tokenUsage") or {}).get("total")) or self.usage
        elif method == "error":
            err = p.get("error") or {}
            msg = err.get("message") or str(err)
            if p.get("willRetry"):
                s.status("retrying")
                if not self.retry_noted:
                    self.retry_noted = True
                    s.notice("warning", f"Codex 가 API 오류 뒤 자동으로 다시 시도하는 중입니다: {msg}")
            else:
                self.error = msg + (" — CLI 탭에서 Codex 에 다시 로그인하세요." if err.get("codexErrorInfo") == "unauthorized" else "")
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            status = turn.get("status")
            err = turn.get("error") or {}
            self.turn_id = None
            u = self.usage or {}
            error = self.error or (err.get("message") if isinstance(err, dict) else None)
            s.completed({
                "subtype": status or "completed", "is_error": status == "failed", "interrupted": status == "interrupted",
                "duration_ms": turn.get("durationMs") or int((time.time() - self.started_at) * 1000),
                "usage": usage_out(u.get("inputTokens"), u.get("outputTokens"), u.get("cachedInputTokens")),
                "errors": [error] if status == "failed" and error else [], "result": None,
            })
        elif method == "configWarning" or method == "warning":
            if msg := (p.get("message") or p.get("summary")):
                s.notice("warning", f"Codex: {msg}")

    async def _on_request(self, method: str, p: dict[str, Any]) -> Any:
        if method == "item/commandExecution/requestApproval":
            iid = str(p.get("itemId") or uuid.uuid4().hex)
            inp = {"command": _codex_command(p.get("command")), "description": p.get("cwd") or "",
                   **({"reason": p["reason"]} if p.get("reason") else {})}
            return {"decision": _codex_decision(await self.sink.ask("Bash", iid, inp, "command"))}
        if method == "item/fileChange/requestApproval":
            iid = str(p.get("itemId") or uuid.uuid4().hex)
            name, inp = _codex_patch(self.changes.get(iid) or [])
            if p.get("reason"):
                inp["reason"] = p["reason"]
            return {"decision": _codex_decision(await self.sink.ask(name, iid, inp, "edit"))}
        if method in ("execCommandApproval", "applyPatchApproval"):   # 예전 방식 — 묻지 않고 거절 (새 방식으로 다시 묻는다)
            return {"decision": "denied"}
        raise RpcError({"code": -32601, "message": f"지원하지 않는 요청입니다: {method}"})


def _codex_command(cmd: Any) -> str:
    """'"C:\\…\\powershell.exe" -Command 'echo hi'' → 'echo hi' (보기 좋게). 그 밖에는 그대로."""
    text = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
    m = re.match(r'^\s*"?[^"]*?(?:powershell|pwsh)(?:\.exe)?"?\s+(?:-NoProfile\s+)?-Command\s+(.+)$', text, re.I | re.S)
    if m:
        inner = m.group(1).strip()
        if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
            inner = inner[1:-1]
        return inner
    return text


def _codex_patch(changes: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Codex 파일 변경 → 화면 도구 카드. 새 파일 하나면 Write, 그 밖에는 Patch(파일별 diff)."""
    if len(changes) == 1 and ((changes[0].get("kind") or {}).get("type") == "add"):
        c = changes[0]
        return "Write", {"file_path": c.get("path") or "", "content": c.get("diff") or ""}
    return "Patch", {"changes": [{"path": c.get("path") or "", "kind": (c.get("kind") or {}).get("type") or "update",
                                  "move_path": (c.get("kind") or {}).get("move_path"), "diff": c.get("diff") or ""}
                                 for c in changes]}


def _codex_decision(d: str) -> str:
    return {"allow": "accept", "interrupt": "cancel"}.get(d, "decline")


# ───────────────────────── Gemini

class GeminiDriver(Driver):
    engine = "gemini"
    PREFIX = "<agent-server-instructions>"

    def __init__(self, sink: Sink, cli: EngineCli, cwd: Path):
        super().__init__(sink, cli, cwd)
        self.session_id: str | None = None
        self.instructions = ""
        self.needs_instructions = False
        self.loading = False
        self.open: dict[str, str] = {}          # 열린 글 블록: text/thinking → key
        self.texts: dict[str, str] = {}
        self.seen_tools: set[str] = set()
        self.done_tools: set[str] = set()
        self.prompt_task: asyncio.Task | None = None
        self.counter = itertools.count(1)

    async def start(self, *, resume: str | None, instructions: str, model: str | None, effort: str | None, mode: str,
                    dirs: list[str]) -> str:
        self.model, self.mode, self.instructions = model or None, mode, instructions
        if not gemini_auth()["logged_in"]:
            raise EngineError("not_logged_in", "Gemini 로그인이 필요합니다 — 관리 › 외부 AI 모델 › CLI 의 Gemini '로그인'을 누르세요.", 409)
        argv = [*self.cli.argv, "--acp"]
        if dirs := [d for d in dirs if d and Path(d).is_dir()]:
            argv += ["--include-directories", ",".join(dirs)]   # 하네스 · 첨부 폴더도 읽을 수 있게
        self.rpc = JsonRpcProcess(argv, cwd=self.cwd, env=_env({"GEMINI_CLI_NO_RELAUNCH": "true"}),
                                  on_notification=self._on_note, on_request=self._on_request, on_exit=self.sink.exited)
        try:
            await self.rpc.start()
            await self.rpc.request("initialize", {"protocolVersion": 1, "clientInfo": CLIENT_INFO,
                                                  "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                                                         "terminal": False}}, 45)
        except asyncio.TimeoutError as e:
            await self.rpc.close(3)
            raise EngineError("not_logged_in", "Gemini 가 로그인(브라우저)을 기다리느라 시작하지 못했습니다 — CLI 탭에서 다시 로그인하세요.", 409) from e
        except Exception as e:  # noqa: BLE001
            await self.rpc.close(3)
            raise self._fail_start("CLI 를 시작하지 못했습니다", e) from e
        r = None
        try:
            if resume:
                self.loading = True   # 기록을 session/update 로 다시 흘려보낸다 — 화면 기록은 우리 DB 에 있으므로 버린다
                try:
                    r = await self.rpc.request("session/load", {"sessionId": resume, "cwd": str(self.cwd), "mcpServers": []}, 180)
                    self.session_id = resume
                except RpcError as e:
                    self.sink.notice("warning", f"이전 Gemini 대화를 이어받지 못해 새로 시작합니다 (화면 기록은 그대로): {e.message}")
                finally:
                    self.loading = False
            if self.session_id is None:
                r = await self.rpc.request("session/new", {"cwd": str(self.cwd), "mcpServers": []}, 120)
                self.session_id = (r or {}).get("sessionId")
                self.needs_instructions = True
        except RpcError as e:
            await self.rpc.close(3)
            if e.code == -32000:
                raise EngineError("not_logged_in", f"Gemini 로그인이 필요합니다 ({e.message}) — CLI 탭에서 로그인하세요.", 409) from e
            raise self._fail_start("대화를 시작하지 못했습니다", e) from e
        except Exception as e:  # noqa: BLE001
            await self.rpc.close(3)
            raise self._fail_start("대화를 시작하지 못했습니다", e) from e
        if not self.session_id:
            await self.rpc.close(3)
            raise EngineError("cli_start_failed", "Gemini 가 대화 id 를 주지 않았습니다")
        models = ((r or {}).get("models") or {})
        avail = [{"id": m.get("modelId"), "label": m.get("name") or m.get("modelId"), "default": m.get("modelId") == models.get("currentModelId")}
                 for m in models.get("availableModels") or [] if isinstance(m, dict) and m.get("modelId")]
        if avail:
            self.sink.remember_models(avail)
        try:
            if self.model:
                await self.rpc.request("session/set_model", {"sessionId": self.session_id, "modelId": self.model}, 30)
            else:
                self.model = models.get("currentModelId")
            if mode == "plan":
                await self.rpc.request("session/set_mode", {"sessionId": self.session_id, "modeId": "plan"}, 30)
        except RpcError as e:
            self.sink.notice("warning", f"Gemini 설정을 바로 적용하지 못했습니다: {e.message}")
        return self.session_id

    async def send(self, text: str, images: list[dict[str, Any]], first: bool) -> None:
        assert self.rpc and self.session_id
        self.open.clear()
        self.texts.clear()
        self.started_at = time.time()
        body = text
        if self.needs_instructions and self.instructions:
            body = (f"{self.PREFIX}\n아래는 이 서버 관리자가 정한 지침이다. 이 대화 내내 따른다 (사용자에게 다시 보여 주지 않는다).\n\n"
                    f"{self.instructions}\n</agent-server-instructions>\n\n{text}")
        self.needs_instructions = False
        blocks: list[dict[str, Any]] = [{"type": "text", "text": body}] if body else []
        blocks += [{"type": "image", "mimeType": im["mime"], "data": im["data"]} for im in images]
        self.prompt_task = asyncio.create_task(self._prompt(blocks))

    async def _prompt(self, blocks: list[dict[str, Any]]) -> None:
        assert self.rpc
        try:
            r = await self.rpc.request("session/prompt", {"sessionId": self.session_id, "prompt": blocks}, None) or {}
            self._close_blocks()
            stop = r.get("stopReason")
            u = r.get("usage") or {}
            self.sink.completed({
                "subtype": stop or "end_turn", "is_error": False, "interrupted": stop == "cancelled",
                "duration_ms": int((time.time() - self.started_at) * 1000),
                "usage": usage_out(u.get("inputTokens"), u.get("outputTokens"), u.get("cachedReadTokens")),
                "errors": [], "result": None,
                **({"terminal_reason": "max_tokens"} if stop in ("max_tokens", "max_turn_requests") else {}),
            })
        except ConnectionError:
            return   # 프로세스가 끝남 — exited 가 알린다
        except Exception as e:  # noqa: BLE001
            self._close_blocks()
            msg = getattr(e, "message", None) or str(e)
            details = (getattr(e, "data", None) or {}).get("details") if isinstance(getattr(e, "data", None), dict) else None
            self.sink.completed({"subtype": "error", "is_error": True, "interrupted": False,
                                 "duration_ms": int((time.time() - self.started_at) * 1000), "usage": None,
                                 "errors": [f"{msg}" + (f" ({details})" if details else "")], "result": None})

    async def interrupt(self) -> None:
        if self.rpc and self.session_id:
            await self.rpc.notify("session/cancel", {"sessionId": self.session_id})

    async def set_mode(self, mode: str) -> None:
        self.mode = mode
        if self.rpc and self.session_id:
            await self.rpc.request("session/set_mode", {"sessionId": self.session_id, "modeId": "plan" if mode == "plan" else "default"}, 30)

    async def set_model(self, model: str | None) -> None:
        self.model = model or None
        if self.rpc and self.session_id and model:
            await self.rpc.request("session/set_model", {"sessionId": self.session_id, "modelId": model}, 30)

    # ---- 스트리밍
    def _chunk(self, btype: str, text: str) -> None:
        other = "thinking" if btype == "text" else "text"
        if other in self.open:
            self._close(other)
        key = self.open.get(btype)
        if key is None:
            key = self.open[btype] = f"g{next(self.counter)}-{uuid.uuid4().hex[:6]}"
            self.texts[key] = ""
            self.sink.block_start(key, btype)
        self.texts[key] += text
        self.sink.delta(key, text)

    def _close(self, btype: str) -> None:
        key = self.open.pop(btype, None)
        if key is None:
            return
        text = self.texts.pop(key, "")
        if text.strip():
            self.sink.message(assistant([{"type": btype, "text": text}], self.model), replaces_partial=key)
        else:
            self.sink.drop_partial(key)

    def _close_blocks(self) -> None:
        for btype in ("thinking", "text"):
            self._close(btype)

    def _tool_started(self, call: dict[str, Any]) -> None:
        tid = str(call.get("toolCallId") or uuid.uuid4().hex)
        if tid in self.seen_tools:
            return
        self.seen_tools.add(tid)
        self._close_blocks()
        name, inp, _ = _gemini_tool(call)
        self.sink.message(assistant([tool_use(tid, name, inp)], self.model))

    def _tool_finished(self, call: dict[str, Any]) -> None:
        tid = str(call.get("toolCallId") or "")
        if not tid or tid in self.done_tools:
            return
        self.done_tools.add(tid)
        if tid not in self.seen_tools:
            self._tool_started(call)
        texts = []
        for c in call.get("content") or []:
            if c.get("type") == "content" and (c.get("content") or {}).get("type") == "text":
                texts.append(c["content"].get("text") or "")
            elif c.get("type") == "diff":
                texts.append(f"변경: {c.get('path')}")
        status = call.get("status")
        self.sink.message(results([tool_result(tid, "\n".join(texts) or ("완료" if status == "completed" else str(status)),
                                               status == "failed")]))

    def _on_note(self, method: str, p: dict[str, Any]) -> None:
        if method != "session/update" or self.loading:
            return
        u = p.get("update") or {}
        kind = u.get("sessionUpdate")
        content = u.get("content") or {}
        if kind == "agent_message_chunk" and content.get("type") == "text":
            text = content.get("text") or ""
            if text.startswith("[MODE_UPDATE]"):
                return
            self._chunk("text", text)
        elif kind == "agent_thought_chunk" and content.get("type") == "text":
            self._chunk("thinking", content.get("text") or "")
        elif kind == "tool_call":
            self._tool_started(u)
            if u.get("status") in ("completed", "failed"):
                self._tool_finished(u)
        elif kind == "tool_call_update" and u.get("status") in ("completed", "failed"):
            self._tool_finished(u)

    async def _on_request(self, method: str, p: dict[str, Any]) -> Any:
        if method == "session/request_permission":
            call = p.get("toolCall") or {}
            self._tool_started(call)
            tid = str(call.get("toolCallId") or uuid.uuid4().hex)
            name, inp, category = _gemini_tool(call)
            decision = await self.sink.ask(name, tid, inp, category)
            options = p.get("options") or []
            if decision == "interrupt":
                asyncio.create_task(self.interrupt())
                return {"outcome": {"outcome": "cancelled"}}
            want = "allow_once" if decision == "allow" else "reject_once"
            opt = next((o for o in options if o.get("kind") == want), None) \
                or next((o for o in options if o.get("optionId") == ("proceed_once" if decision == "allow" else "cancel")), None)
            if opt is None:
                return {"outcome": {"outcome": "cancelled"}}
            return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
        raise RpcError({"code": -32601, "message": f"지원하지 않는 요청입니다: {method}"})


def _gemini_tool(call: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    """ACP 도구 호출 → (화면 도구 이름, 입력, 승인 종류 edit · command · other)."""
    kind = call.get("kind") or "other"
    title = str(call.get("title") or "")
    locs = [loc.get("path") for loc in call.get("locations") or [] if isinstance(loc, dict) and loc.get("path")]
    diffs = [c for c in call.get("content") or [] if c.get("type") == "diff"]
    if diffs:
        d = diffs[0]
        if len(diffs) == 1 and not d.get("oldText"):
            return "Write", {"file_path": d.get("path") or (locs[0] if locs else ""), "content": d.get("newText") or ""}, "edit"
        if len(diffs) == 1:
            return "Edit", {"file_path": d.get("path") or "", "old_string": d.get("oldText") or "", "new_string": d.get("newText") or ""}, "edit"
        return "MultiEdit", {"file_path": d.get("path") or "", "edits": [{"old_string": x.get("oldText") or "", "new_string": x.get("newText") or ""}
                                                                         for x in diffs]}, "edit"
    if kind == "edit" or kind == "delete" or kind == "move":
        return "Edit", {"file_path": locs[0] if locs else title, "old_string": "", "new_string": "", "description": title}, "edit"
    if kind == "execute":
        return "Bash", {"command": title}, "command"
    if kind == "read":
        return "Read", {"file_path": locs[0] if locs else title}, "other"
    if kind == "search":
        return "Grep", {"pattern": title, **({"path": locs[0]} if locs else {})}, "other"
    if kind == "fetch":
        return "WebFetch", {"url": title}, "other"
    return title or kind, {"kind": kind, **({"paths": locs} if locs else {})}, "other"


# ───────────────────────── 만들기

def make_driver(engine: str, sink: Sink, cwd: Path, override: str = "") -> Driver:
    if engine not in ("codex", "gemini"):
        raise EngineError("bad_engine", f"모르는 엔진입니다: {engine}", 400)
    cli = locate(engine, override)
    if not cli.found:
        raise EngineError("cli_not_found", cli.error or f"{LABELS[engine]} CLI 가 없습니다", 409)
    return (CodexDriver if engine == "codex" else GeminiDriver)(sink, cli, cwd)
