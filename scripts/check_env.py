"""0단계 환경 점검 (폐쇄망 PC 에서도 그대로 쓴다).

    .venv\\Scripts\\python scripts\\check_env.py          # 파일·패키지·GPU 점검
    .venv\\Scripts\\python scripts\\check_env.py --load   # llama-server 를 띄워 실제 응답·속도까지 확인
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAI  # noqa: E402

from agent.config import load_settings  # noqa: E402
from agent.runtime import LlamaServer, health, props  # noqa: E402

OK, BAD, WARN = "\033[32m✔\033[0m", "\033[31m✘\033[0m", "\033[33m!\033[0m"
problems = 0


def report(ok: bool | None, label: str, detail: str = "") -> None:
    global problems
    mark = OK if ok else (WARN if ok is None else BAD)
    problems += ok is False
    print(f" {mark} {label}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", action="store_true")
    args = ap.parse_args()
    s = load_settings()

    print("[Python]")
    report(sys.version_info >= (3, 12), "Python", sys.version.split()[0])
    report(".venv" in sys.prefix, "가상환경(.venv) 사용 중", sys.prefix)
    for mod in ["openai", "pydantic", "yaml", "json_repair", "psutil", "fastapi", "httpx", "trafilatura", "pymupdf", "docx", "sqlite_vec", "apscheduler"]:
        try:
            importlib.import_module(mod)
            report(True, f"import {mod}")
        except ImportError as e:
            report(False, f"import {mod}", str(e))

    print("[SQLite]")
    try:
        import sqlite_vec
        db = sqlite3.connect(":memory:")
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        report(True, "sqlite-vec", db.execute("select vec_version()").fetchone()[0])
        db.execute("create virtual table t using fts5(x, tokenize='trigram')")
        report(True, "FTS5 trigram (한국어 부분 검색용)")
    except Exception as e:
        report(False, "sqlite-vec / FTS5", str(e))

    print("[GPU]")
    if shutil.which("nvidia-smi"):
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,driver_version",
                              "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
        report(True, "nvidia-smi", out)
    else:
        report(False, "nvidia-smi 없음", "NVIDIA 드라이버를 설치하세요 (CUDA 13 빌드는 R580 이상 드라이버 필요)")

    print("[llama.cpp]")
    server = LlamaServer(s)
    exe, model = s.path(s.runtime.exe), s.path(s.runtime.model_path)
    report(exe.exists(), "llama-server.exe", str(exe))
    report(model.exists(), "모델 파일", f"{model} ({model.stat().st_size / 2**30:.2f} GB)" if model.exists() else str(model))
    state = health(s)
    report(True if state == "ok" else None, f"서버 상태: {state}", s.backend.base_url)

    if args.load:
        print("[실제 응답 테스트]")
        started_here = False
        if state == "down":
            try:
                server.start()
                started_here = True
            except RuntimeError as e:
                report(False, "llama-server 시작", str(e))
                return 1
            t0 = time.monotonic()
            ready = server.wait_ready(300)
            report(ready, "모델 적재", f"{time.monotonic() - t0:.1f}s" if ready else "실패 — 아래 로그 확인")
            if not ready:
                print(server.log_tail(40))
                server.stop()
                return 1
        p = props(s)
        report(True, "실제 컨텍스트", str(p.get("n_ctx")))
        client = OpenAI(base_url=s.backend.base_url, api_key=s.backend.api_key)
        t0 = time.monotonic()
        r = client.chat.completions.create(
            model=s.backend.model, messages=[{"role": "user", "content": "한 문장으로 자기소개 해줘."}],
            max_tokens=200, extra_body={"chat_template_kwargs": {"enable_thinking": False}})
        dt = time.monotonic() - t0
        n = r.usage.completion_tokens if r.usage else 0
        report(bool(r.choices[0].message.content), "응답", (r.choices[0].message.content or "").strip()[:80])
        report(True, "속도", f"{n} 토큰 / {dt:.1f}s ≈ {n / dt:.1f} tok/s (첫 토큰 지연 포함)")
        if shutil.which("nvidia-smi"):
            used = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader"],
                                  capture_output=True, text=True).stdout.strip()
            report(True, "VRAM 사용", used)
        if started_here:
            server.stop()

    print("\n문제 없음" if not problems else f"\n문제 {problems}건")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
