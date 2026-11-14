"""테스트 공용 도구. FakeLLM 은 정해진 응답을 순서대로 돌려주는 가짜 모델이다.
진짜 모델 없이 루프·안전장치·도구 로직을 검증할 수 있다.
"""

from __future__ import annotations

import copy
import itertools
from pathlib import Path
from typing import Any, Callable

import pytest

from agent.config import AllowedDir, Limits, Settings
from agent.llm import LLMResponse, ToolCallRequest
from agent.profiles import Profile
from agent.tools import ToolContext
from agent.safety import Sandbox

_ids = itertools.count(1)


def call(tool_name: str, /, **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(id=f"call_{next(_ids)}", name=tool_name, arguments=arguments)


def reply(*calls: ToolCallRequest, content: str = "", reasoning: str = "") -> LLMResponse:
    return LLMResponse(content=content, reasoning=reasoning, tool_calls=list(calls),
                       prompt_tokens=10, completion_tokens=5)


class FakeLLM:
    model = "fake"

    def __init__(self, script: list[LLMResponse | Callable[[list[dict]], LLMResponse]]):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, think=None) -> LLMResponse:
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, "think": think})
        if not self.script:
            return reply(content="(스크립트 끝)")
        item = self.script.pop(0)
        return item(messages) if callable(item) else item


@pytest.fixture(autouse=True)
def _default_prompts(tmp_path_factory, monkeypatch):
    """관리 화면에서 바꾼 프롬프트(config/prompts.yaml)가 테스트 결과를 바꾸지 않게, 테스트는 기본값으로 돈다."""
    from agent import prompts
    monkeypatch.setattr(prompts, "USER_FILE", tmp_path_factory.mktemp("prompts") / "prompts.yaml")


@pytest.fixture(autouse=True)
def _isolated_accounts(tmp_path_factory, monkeypatch):
    """접속 설정(config/access.yaml)·사용자별 계정·계정 DB·실행 기록 DB·하네스는 테스트마다 비어 있는 것으로
    (기본 = 혼자 쓰기, 로그인 없음). 진짜 config/ · data/ · harness/ 는 어떤 테스트도 건드리지 않는다."""
    from agent import accounts
    d = tmp_path_factory.mktemp("access")
    monkeypatch.setattr(accounts, "access_file", lambda: d / "access.yaml")
    monkeypatch.setattr(accounts, "user_secrets_file", lambda: d / "user_secrets.yaml")
    accounts.save_access(accounts.Access(login_enabled=False))   # 대부분의 테스트는 혼자 쓰기로 (로그인 먼저는 test_multiuser)
    from agent import harness   # 하네스(관리자 마크다운)도 테스트마다 빈 폴더
    monkeypatch.setattr(harness, "HARNESS_DIR", d / "harness")
    # 서버 모듈을 테스트 안에서 늦게 불러와도 진짜 data/accounts.db · agent.db 를 건드리지 않게 늘 임시 DB 로 바꿔 끼운다
    # (테스트가 deps.store 를 직접 바꿔 끼우면 그쪽이 이긴다 — 이 픽스처가 먼저 돈다)
    from agent.store import Store
    from server import deps
    monkeypatch.setattr(deps, "accounts", accounts.AccountStore(d / "accounts.db"))
    monkeypatch.setattr(deps, "store", Store(d / "agent.db"))
    accounts.CURRENT.set(None)
    from agent import providers   # '연결 확인'의 짧은 실제 호출(잔액 확인)은 테스트에서 하지 않는다 (test_providers 가 따로 시험)
    monkeypatch.setattr(providers, "_probe", lambda *a, **k: None)
    from agent.claude_code import engines   # 이 PC 의 진짜 Codex · Gemini 는 찾지도 띄우지도 않는다 (test_cli_engines 는 가짜 CLI)
    monkeypatch.setattr(engines, "locate", lambda engine, override="": engines.EngineCli(engine, False, error="시험: CLI 없음"))
    engines.status_cache.clear()


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    p = tmp_path / "ws"
    p.mkdir()
    return p


@pytest.fixture
def settings(tmp_path: Path, ws: Path) -> Settings:
    return Settings(
        workspace=str(ws),
        session_folders=False,   # 대부분의 테스트는 작업 폴더 바로 아래에서 확인한다 (세션 폴더는 test_session.py)
        allowed_dirs=[AllowedDir(path=str(ws), mode="rw")],
        data_dir=str(tmp_path / "data"),
        limits=Limits(max_steps=6, tool_output_chars=4000, command_timeout=30),
    )


@pytest.fixture
def profile() -> Profile:
    return Profile(name="test", system_prompt="테스트용 에이전트",
                   tools=["list_dir", "read_file", "search_files", "write_file", "run_command", "run_python"])


@pytest.fixture
def ctx(settings: Settings, tmp_path: Path) -> ToolContext:
    return ToolContext(settings, Sandbox.from_settings(settings), tmp_path / "run")
