"""문서 Q&A 의 의미 검색용 임베딩 — Qwen3-Embedding-0.6B (GGUF) 를 llama-server --embedding 으로 하나 더 띄운다.

- 채팅 모델과 VRAM 을 다투지 않게 기본은 CPU (settings.embedding.gpu_layers = 0). 0.6B 라 문서 수천 조각도 CPU 로 된다.
- 처음 쓸 때(색인 · 검색) 띄우고, 관리 서버가 끝날 때 같이 끈다 (작업 개체 — 창을 닫아도 고아로 남지 않는다).
- OpenAI 호환 /v1/embeddings 로 받고 길이 1 로 맞춘다 (sqlite-vec 의 코사인 거리).
- Qwen3-Embedding 은 질문에만 지시문을 붙인다 ("Instruct: …\\nQuery: …"), 문서 조각은 그대로.
- 모델 파일이 없거나 꺼 두면 get_embedder 가 None → 문서 검색은 낱말 검색(FTS5)만 한다.
"""
from __future__ import annotations

import math
import subprocess
import threading
import time
from pathlib import Path
from typing import Protocol

import httpx

from agent.config import Settings
from agent.proc import job_add, kill_tree

BATCH = 16


class EmbeddingError(RuntimeError):
    pass


class Embedder(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def server_command(settings: Settings) -> list[str]:
    e = settings.embedding
    return [str(settings.path(settings.runtime.exe)), "-m", str(settings.path(e.model_path)),
            "--embedding", "--pooling", "last", "-ngl", str(e.gpu_layers),
            "--host", e.host, "--port", str(e.port), "-c", str(e.ctx), "-b", str(e.ctx), "-ub", str(e.ctx),
            "--alias", "embedding"]


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


_server: subprocess.Popen | None = None
_server_lock = threading.Lock()


def stop_server() -> None:
    """관리 서버가 끝날 때 (server/app.py)."""
    global _server
    with _server_lock:
        if _server is not None and _server.poll() is None:
            kill_tree(_server.pid)
        _server = None


class LlamaEmbedder:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        e = settings.embedding
        self.base_url = (e.url or f"http://{e.host}:{e.port}").rstrip("/")
        self.client = client or httpx.Client(timeout=120)
        self.dim = 0

    # ---- 서버
    def _healthy(self) -> bool:
        try:
            return self.client.get(f"{self.base_url}/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def ensure_server(self, timeout: float = 90) -> None:
        global _server
        if self._healthy():
            return
        if self.settings.embedding.url:
            raise EmbeddingError(f"임베딩 서버({self.base_url})가 응답하지 않습니다")
        with _server_lock:
            if _server is None or _server.poll() is not None:
                exe = self.settings.path(self.settings.runtime.exe)
                if not exe.exists():
                    raise EmbeddingError(f"llama-server 실행 파일이 없습니다: {exe}")
                log = (self.settings.data_path / "logs")
                log.mkdir(parents=True, exist_ok=True)
                out = (log / "embedding-server.log").open("ab")
                _server = subprocess.Popen(server_command(self.settings), stdin=subprocess.DEVNULL, stdout=out,
                                           stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                job_add(_server.pid)
            proc = _server
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._healthy():
                return
            if proc.poll() is not None:
                raise EmbeddingError("임베딩 서버가 시작하지 못했습니다 — data/logs/embedding-server.log 를 보세요")
            time.sleep(0.5)
        raise EmbeddingError("임베딩 서버가 제때 준비되지 않았습니다")

    # ---- 임베딩
    def _post(self, texts: list[str]) -> list[list[float]]:
        self.ensure_server()
        try:
            r = self.client.post(f"{self.base_url}/v1/embeddings", json={"input": texts, "model": "embedding"})
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise EmbeddingError(f"임베딩을 받지 못했습니다: {e}") from e
        vecs = [_unit([float(x) for x in d["embedding"]]) for d in data]
        if vecs:
            self.dim = len(vecs[0])
        return vecs

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            out += self._post(texts[i:i + BATCH])
        return out

    def embed_query(self, text: str) -> list[float]:
        instr = self.settings.embedding.query_instruction
        return self._post([f"Instruct: {instr}\nQuery: {text}"])[0]


def get_embedder(settings: Settings) -> LlamaEmbedder | None:
    """쓸 수 있으면 임베더, 아니면 None (꺼 둠 · 모델 파일 없음 → 낱말 검색만)."""
    e = settings.embedding
    if not e.enabled:
        return None
    if not e.url and not Path(settings.path(e.model_path)).is_file():
        return None
    return LlamaEmbedder(settings)
