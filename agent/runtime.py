"""llama.cpp 의 llama-server 프로세스 관리.

llama-server 는 exe 하나 + GGUF 모델 파일 하나로 OpenAI 호환 API(/v1/chat/completions)를 제공한다.
설치·인터넷·백그라운드 서비스가 필요 없어서 폐쇄망에 파일만 복사해 가면 된다.
처음 실행할 때 llama-server.exe 가 없으면 runtime/downloads 의 zip 을 자동으로 푼다.

관리 서버 창을 그냥 닫으면 정상 종료 코드가 돌지 않아 llama-server 가 고아로 남아 VRAM 을 계속 잡는다.
그래서 Windows Job 객체에 넣어 '관리 서버 프로세스가 죽으면 같이 죽게' 하고, 이미 남아 있는 고아는 이어받는다.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
import psutil

from agent.config import Settings
from agent.proc import KillOnCloseJob, job_add as _job_add, kill_tree  # noqa: F401  (KillOnCloseJob: 테스트·하위 호환)

DOWNLOADS_DIR = "runtime/downloads"


def server_root(settings: Settings) -> str:
    return settings.backend.base_url.rstrip("/").removesuffix("/v1")


def build_argv(settings: Settings) -> list[str]:
    rt = settings.runtime
    argv = [
        str(settings.path(rt.exe)),
        "-m", str(settings.path(rt.model_path)),
        "--alias", settings.backend.model,
        "--host", rt.host, "--port", str(rt.port),
        "-c", str(settings.backend.context_length),
        "-ngl", str(rt.gpu_layers),
        "-np", "1",   # 슬롯 1개: 컨텍스트 전체를 한 대화에 쓴다 (여러 개면 컨텍스트가 나뉜다)
        "--jinja",    # 모델에 내장된 채팅 템플릿 사용 → 도구 호출 파싱에 필요
    ]
    if rt.flash_attn:
        argv += ["--flash-attn", "on"]
    if rt.cache_type:
        argv += ["--cache-type-k", rt.cache_type, "--cache-type-v", rt.cache_type]
    return argv + list(rt.extra_args)


def health(settings: Settings) -> str:
    """'ok' | 'loading' | 'down' | 'occupied'(그 포트에서 llama-server 가 아닌 다른 프로그램이 응답함)"""
    try:
        r = httpx.get(f"{server_root(settings)}/health", timeout=2)
    except httpx.HTTPError:
        return "down"
    try:
        data = r.json()
    except ValueError:
        data = None
    # llama-server 는 {"status":"ok"} 또는 503 + {"error":{...}} 를 JSON 으로 준다. 아니면 남의 서버 (예: Docker 의 nginx)
    if r.status_code == 200 and isinstance(data, dict) and data.get("status") == "ok":
        return "ok"
    if r.status_code == 503 and isinstance(data, dict):
        return "loading"
    return "occupied"


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _owner_alive(p: psutil.Process) -> bool:
    """이 llama-server 를 띄운 프로세스(다른 관리 서버)가 아직 살아 있는지. 나 자신이 띄운 것은 아니라고 본다."""
    try:
        parent = p.parent()
        return parent is not None and parent.pid != os.getpid() and parent.is_running() \
            and parent.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def free_port(host: str, start: int, tries: int = 50) -> int:
    for port in range(start, start + tries):
        if not port_in_use(host, port):
            return port
    raise RuntimeError(f"{start}~{start + tries - 1} 사이에 빈 포트가 없습니다")


def props(settings: Settings) -> dict[str, Any]:
    """실행 중인 서버의 실제 설정 (컨텍스트 크기, 모델 경로 등)."""
    try:
        data = httpx.get(f"{server_root(settings)}/props", timeout=3).json()
    except (httpx.HTTPError, ValueError):
        return {}
    gen = data.get("default_generation_settings") or {}
    return {"n_ctx": gen.get("n_ctx") or data.get("n_ctx"), "model_path": data.get("model_path"),
            "build": data.get("build_info")}


def runtime_zips(settings: Settings) -> list[Path]:
    return sorted(settings.path(DOWNLOADS_DIR).glob("*.zip"))


def unpack_runtime(settings: Settings) -> list[str]:
    """runtime/downloads/*.zip (llama.cpp 본체 + CUDA 런타임) 을 llama-server.exe 가 있어야 할 폴더에 푼다."""
    dest = settings.path(settings.runtime.exe).parent
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    names = []
    for z in runtime_zips(settings):
        with zipfile.ZipFile(z) as zf:
            for member in zf.infolist():  # '../' 로 폴더 밖에 파일을 쓰는 압축 파일(zip slip) 차단
                if not (dest / member.filename).resolve().is_relative_to(root):
                    raise RuntimeError(f"압축 파일에 허용되지 않는 경로가 있습니다: {z.name}: {member.filename}")
            zf.extractall(dest)
        names.append(z.name)
    return names


class LlamaServer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.proc: subprocess.Popen | None = None
        self.log_path = settings.data_path / "logs" / "llama-server.log"
        self.stage = ""   # 화면에 보여줄 준비 단계 (예: 압축 해제 중)
        self.error = ""   # 마지막 시작 실패 이유
        self.adopted: psutil.Process | None = None   # 이전 관리 서버가 남긴 llama-server 를 이어받은 경우
        self.notice = ""  # 사용자에게 알릴 일 (예: 포트를 바꿨음)

    def problems(self) -> list[str]:
        rt, out = self.settings.runtime, []
        if not self.settings.path(rt.exe).exists() and not runtime_zips(self.settings):
            out.append(f"llama-server 실행 파일이 없습니다: {self.settings.path(rt.exe)} "
                       f"(llama.cpp zip 을 {self.settings.path(DOWNLOADS_DIR)} 에 넣으면 자동으로 풉니다)")
        if not self.settings.path(rt.model_path).exists():
            out.append(f"모델 파일이 없습니다: {self.settings.path(rt.model_path)}")
        return out

    @property
    def running(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        try:
            return self.adopted is not None and self.adopted.is_running()
        except psutil.Error:
            return False

    def find_orphan(self) -> psutil.Process | None:
        """우리 llama-server.exe 이면서 우리 포트를 듣고 있는 프로세스 (창을 닫아 고아가 된 것).
        띄운 관리 서버가 아직 살아 있으면 고아가 아니다 — 이어받지 않는다 (이어받으면 이 프로세스가 끝날 때 같이 꺼진다:
        이미 켜진 서버 옆에서 start.cmd 를 또 눌러 포트가 겹쳐 끝나면서 쓰던 모델 서버까지 끈 일이 있었다, 2026-11-13)."""
        exe = os.path.normcase(os.path.realpath(self.settings.path(self.settings.runtime.exe)))
        for p in psutil.process_iter(["exe"]):
            try:
                if not p.info["exe"] or os.path.normcase(os.path.realpath(p.info["exe"])) != exe:
                    continue
                if _owner_alive(p):
                    continue
                if any(c.status == psutil.CONN_LISTEN and c.laddr.port == self.settings.runtime.port
                       for c in p.net_connections(kind="tcp")):
                    return p
            except psutil.Error:
                continue
        return None

    def adopt(self) -> bool:
        """남아 있는 llama-server 를 이어받는다. 다른 모델을 올리고 있으면 지금 설정한 모델로 다시 띄운다."""
        orphan = self.find_orphan()
        if orphan is None:
            return False
        self.adopted = orphan
        _job_add(orphan.pid)   # 이제부터는 이 관리 서버가 끝날 때 같이 끝난다
        try:
            loaded = " ".join(orphan.cmdline())
        except psutil.Error:
            loaded = ""
        wanted = str(self.settings.path(self.settings.runtime.model_path))
        if os.path.normcase(wanted) not in os.path.normcase(loaded) or f"--alias {self.settings.backend.model} " not in loaded + " ":
            print(f"남아 있던 모델 서버가 다른 모델을 쓰고 있어 '{self.settings.backend.model}' 로 다시 띄웁니다.")
            self.stage = f"'{self.settings.backend.model}' 모델로 바꾸는 중"
            try:
                self.stop()
                self.start()
            finally:
                self.stage = ""
        else:
            print("남아 있던 모델 서버를 이어받았습니다.")
        return True

    def adopt_async(self) -> None:
        """관리 화면이 바로 뜨도록 이어받기(고아 찾기·모델 교체)를 백그라운드에서 한다."""
        self.stage = "모델 서버 확인 중"

        def work() -> None:
            try:
                self.adopt()
            except Exception as e:
                self.error = str(e)
                print(f"모델 서버를 이어받지 못했습니다: {e}")
            finally:
                if self.stage == "모델 서버 확인 중":
                    self.stage = ""

        threading.Thread(target=work, daemon=True).start()

    def start_async(self) -> None:
        """화면이 멈추지 않도록 별도 스레드에서 (압축 해제 →) 시작한다. 실패 이유는 self.error 에 남는다."""
        if self.running or self.stage:
            return
        self.error = ""

        def work() -> None:
            try:
                self.start()
            except Exception as e:
                self.error = str(e)
                print(f"llama-server 를 시작하지 못했습니다: {e}")

        threading.Thread(target=work, daemon=True).start()

    def start(self) -> None:
        if self.running:
            return
        if problems := self.problems():
            raise RuntimeError("; ".join(problems))
        if not self.settings.path(self.settings.runtime.exe).exists():
            self.stage = "llama.cpp 압축 해제 중 (처음 한 번, 1분 이내)"
            print(f"{self.stage}...")
            try:
                unpack_runtime(self.settings)
            finally:
                self.stage = ""
            if not self.settings.path(self.settings.runtime.exe).exists():
                raise RuntimeError(f"압축을 풀었지만 {self.settings.path(self.settings.runtime.exe)} 가 없습니다. zip 내용을 확인하세요")
        rt = self.settings.runtime
        if port_in_use(rt.host, rt.port):   # 다른 프로그램(Docker 등)이 포트를 쓰고 있으면 빈 포트로 바꾼다
            new = free_port(rt.host, rt.port + 1)
            from agent import connections
            connections.set_runtime_port(new)
            self.notice = f"포트 {rt.port} 를 다른 프로그램이 쓰고 있어 {new} 로 바꿨습니다"
            print(self.notice)
            rt.port = new
            self.settings.sync_local_backend()
        print(f"llama-server 시작 중... 포트 {rt.port} (모델 적재에 수십 초 걸릴 수 있음)")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("ab")
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(build_argv(self.settings))}\n".encode())
        log.flush()
        self.proc = subprocess.Popen(build_argv(self.settings), stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _job_add(self.proc.pid)   # 관리 서버 창을 닫아도 고아로 남지 않게

    def wait_ready(self, timeout: float = 300) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if health(self.settings) == "ok":
                return True
            if self.proc is not None and self.proc.poll() is not None:
                return False   # 프로세스가 죽었다 → 로그 확인
            time.sleep(0.5)
        return False

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            kill_tree(self.proc.pid)
        if self.adopted is not None:
            try:
                kill_tree(self.adopted.pid)
            except psutil.Error:
                pass
        self.proc = None
        self.adopted = None

    def log_tail(self, lines: int = 30) -> str:
        if not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
