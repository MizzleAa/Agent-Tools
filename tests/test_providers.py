"""외부 AI 모델: 연결 저장 (agent/providers.py) · Claude 호출 모양 (agent/llm_anthropic.py) · OpenAI/Gemini 요청 모양 (agent/llm.py).
진짜 공급사에는 보내지 않는다 — SDK 클라이언트를 가짜로 바꿔 끼워 보낸 요청과 돌려받은 답을 확인한다."""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest

from agent import accounts, config, providers
from agent.config import Backend, Sampling
from agent.context import shrink_old
from agent.llm import LLMClient, LLMResponse, _parse_tool_call, assistant_message, make_client
from agent.llm_anthropic import AnthropicClient, ProviderAPIError, from_anthropic, to_anthropic
from agent.loop import Agent
from agent.profiles import Profile


# ───────────────────────── 가짜 Anthropic SDK

class Block(dict):
    def model_dump(self, **_):
        return dict(self)


def message(*blocks: dict, stop="end_turn", usage=(100, 20, 0, 0), stop_details=None):
    inp, out, read, write = usage
    return SimpleNamespace(content=[Block(b) for b in blocks], stop_reason=stop, stop_details=stop_details,
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read,
                                                 cache_creation_input_tokens=write))


class FakeAnthropic:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.sent.append(json.loads(json.dumps(kwargs, default=str)))
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def claude(model="claude-opus-5-5", replies=(), **kw) -> tuple[AnthropicClient, FakeAnthropic]:
    fake = FakeAnthropic(replies)
    backend = Backend(provider="anthropic", base_url="https://api.anthropic.com", api_key="sk-test", model=model, **kw)
    return AnthropicClient(backend, Sampling(), client=fake), fake


TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "파일 읽기",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]


# ───────────────────────── Claude: 대화 모양

def test_to_anthropic_shapes_system_tools_and_results():
    msgs = [
        {"role": "system", "content": "지침"},
        {"role": "user", "content": "a.txt 읽어줘"},
        {"role": "assistant", "content": "읽겠습니다", "tool_calls": [
            {"id": "toolu_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}},
            {"id": "toolu_2", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "b.txt"}'}}]},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "A"},
        {"role": "tool", "tool_call_id": "toolu_2", "content": ""},
        {"role": "user", "content": "마무리해"},
        {"role": "system", "content": "중간 지침"},
    ]
    system, out = to_anthropic(msgs)
    assert system == "지침"
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[1]["content"][1] == {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}}
    results = out[2]["content"]   # 병렬 도구 결과는 한 user 메시지에, 뒤이은 사용자 말·중간 지침도 같은 턴에
    assert [b["type"] for b in results] == ["tool_result", "tool_result", "text", "text"]
    assert results[1]["content"] == "(출력 없음)" and results[3]["text"].startswith("[지침]")


def test_raw_claude_blocks_are_sent_back_unchanged():
    raw = [{"type": "thinking", "thinking": "요약", "signature": "SIG=="},
           {"type": "tool_use", "id": "toolu_9", "name": "read_file", "input": {"path": "x"}}]
    _, out = to_anthropic([{"role": "user", "content": "q"},
                           {"role": "assistant", "content": "", "_raw": raw, "tool_calls": []},
                           {"role": "tool", "tool_call_id": "toolu_9", "content": "X"}])
    assert out[1]["content"] == raw


def test_first_message_must_be_user():
    _, out = to_anthropic([{"role": "system", "content": "s"}, {"role": "assistant", "content": "앞 대화"}])
    assert out[0]["role"] == "user"


# ───────────────────────── Claude: 요청 옵션

def test_request_uses_adaptive_thinking_drop_block_cache_and_fallbacks():
    client, _ = claude(effort="high")
    req = client.request([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}], TOOLS)
    assert req["thinking"]["type"] == "adaptive"
    assert req["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert "thinking-binding-controls-2026-08-01" in req["betas"]
    assert req["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["betas"]
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"] == {"effort": "high"}
    assert req["tools"][0]["name"] == "read_file" and "input_schema" in req["tools"][0]
    assert "temperature" not in req and "top_p" not in req and req["max_tokens"] == 16000


def test_request_for_older_models_and_gateways():
    haiku, _ = claude(model="claude-haiku-4-5", effort="high")
    req = haiku.request([{"role": "user", "content": "q"}], None)
    assert "thinking" not in req and "output_config" not in req and "fallbacks" not in req
    fake = FakeAnthropic([])
    gw = AnthropicClient(Backend(provider="anthropic", base_url="https://llm-gw.corp/anthropic", api_key="k",
                                 model="claude-opus-5-5"), Sampling(), client=fake)
    assert "fallbacks" not in gw.request([{"role": "user", "content": "q"}], None)   # 서버 쪽 대체 모델은 직접 연결에서만


def test_final_answer_without_tools_keeps_tool_definitions_and_blocks_tool_use():
    client, _ = claude()
    history = [{"role": "user", "content": "q"},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "toolu_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "toolu_1", "content": "A"}]
    client.request(history, TOOLS)
    req = client.request(history + [{"role": "user", "content": "마무리"}], None)
    assert req["tool_choice"] == {"type": "none"} and req["tools"][0]["name"] == "read_file"


# ───────────────────────── Claude: 답 읽기 · 오류

def test_from_anthropic_reads_text_thinking_tools_and_usage():
    r = from_anthropic(message({"type": "thinking", "thinking": "생각", "signature": "S"},
                               {"type": "text", "text": "읽습니다"},
                               {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a"}},
                               stop="tool_use", usage=(10, 7, 90, 5)))
    assert r.content == "읽습니다" and r.reasoning == "생각" and r.finish_reason == "tool_calls"
    assert r.tool_calls[0].id == "toolu_1" and r.tool_calls[0].arguments == {"path": "a"}
    assert r.prompt_tokens == 105 and r.completion_tokens == 7       # 캐시로 읽은 앞부분까지 입력으로 센다
    assert r.raw[0]["signature"] == "S"


def test_refusal_and_fallback_boundary():
    r = from_anthropic(message(stop="refusal", stop_details=SimpleNamespace(category="cyber")))
    assert "거절" in r.content and "cyber" in r.content and r.finish_reason == "refusal"
    r = from_anthropic(message({"type": "thinking", "thinking": "a", "signature": "x"},
                               {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"}},
                               {"type": "text", "text": "답"}))
    assert [b["type"] for b in r.raw] == ["fallback", "text"]   # 넘어간 지점 앞의 thinking 은 되돌려 보내지 않는다


def _req():
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize("err,expected", [
    (anthropic.APIConnectionError(request=_req()), openai.APIConnectionError),
    (anthropic.BadRequestError("prompt is too long: 250000 tokens", response=httpx2.Response(400, request=_req()), body=None),
     openai.BadRequestError),
    (anthropic.RateLimitError("rate", response=httpx2.Response(429, request=_req()), body=None), ProviderAPIError),
    (anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=_req()), body=None), ProviderAPIError),
])
def test_anthropic_errors_become_openai_style(err, expected):
    client, _ = claude(replies=[err])
    with pytest.raises(expected) as info:
        client.chat([{"role": "user", "content": "q"}])
    if expected is openai.BadRequestError:
        assert "context" in str(info.value)   # 루프가 '컨텍스트 초과'로 알아보고 줄여서 다시 보낸다
    if expected is ProviderAPIError:
        assert info.value.status_code in (401, 429)
        assert ("한도" if info.value.status_code == 429 else "키가 틀렸") in providers.describe_error(info.value)


def test_loop_with_claude_sends_thinking_blocks_back(settings):
    """도구를 부른 다음 요청에 앞 답의 thinking 블록(서명 포함)이 그대로 들어가야 한다 (빼면 400)."""
    (settings.workspace_path / "a.txt").write_text("hello", encoding="utf-8")
    client, fake = claude(replies=[
        message({"type": "thinking", "thinking": "a.txt 를 읽자", "signature": "SIG-1"},
                {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.txt"}}, stop="tool_use"),
        message({"type": "text", "text": "a.txt 내용은 hello 입니다."}),
    ])
    agent = Agent(settings, Profile(name="t", system_prompt="s", tools=["read_file"]), llm=client)
    final = list(agent.run("a.txt 읽어줘"))[-1]
    assert final.status in ("done", "partial") and "hello" in final.answer
    second = fake.sent[1]["messages"]
    assistant = next(m for m in second if m["role"] == "assistant")
    assert assistant["content"][0] == {"type": "thinking", "thinking": "a.txt 를 읽자", "signature": "SIG-1"}
    assert second[-1]["content"][0]["type"] == "tool_result" and "hello" in second[-1]["content"][0]["content"]


def test_shrinking_old_claude_turn_keeps_thinking_and_updates_blocks():
    m = {"role": "assistant", "content": "x" * 3000, "tool_calls": [],
         "_raw": [{"type": "thinking", "thinking": "t", "signature": "S"}, {"type": "text", "text": "x" * 3000}]}
    msgs = [{"role": "system", "content": "s"}, m] + [{"role": "user", "content": "u"}] * 10
    assert shrink_old(msgs, keep_last=2)
    assert m["_raw"][0]["signature"] == "S" and len(m["_raw"][1]["text"]) < 1000


# ───────────────────────── OpenAI · Gemini (OpenAI 호환)

def test_openai_request_omits_local_only_parameters():
    s = Sampling(temperature=0.6, top_p=0.95, top_k=20, min_p=0.0, max_tokens=4000)
    c = LLMClient(Backend(provider="openai", base_url="https://api.openai.com/v1", api_key="k", model="gpt-x", effort="high"), s)
    req = c.request([{"role": "user", "content": "q", "_raw": None}], TOOLS, think=False)
    assert "temperature" not in req and "top_p" not in req and "extra_body" not in req
    assert req["max_completion_tokens"] == 4000 and req["reasoning_effort"] == "high"
    assert "_raw" not in req["messages"][0]
    local = LLMClient(Backend(), s).request([{"role": "user", "content": "q"}], TOOLS, think=False)
    assert local["temperature"] == 0.6 and local["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_gemini_thought_signature_is_kept_and_sent_back():
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="read_file", arguments='{"path":"a"}'),
                         model_extra={"extra_content": {"google": {"thought_signature": "TS=="}}})
    call = _parse_tool_call(tc)
    assert call.extra == {"extra_content": {"google": {"thought_signature": "TS=="}}}
    msg = assistant_message(LLMResponse(content="", tool_calls=[call]))
    assert msg["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "TS=="
    gemini = LLMClient(Backend(provider="gemini", base_url=providers.KINDS["gemini"]["base_url"], api_key="k",
                               model="gemini-x"), Sampling())
    assert gemini.request([msg], None)["messages"][0]["tool_calls"][0]["extra_content"]
    local = LLMClient(Backend(), Sampling())
    assert "extra_content" not in local.request([msg], None)["messages"][0]["tool_calls"][0]   # 로컬 서버에는 빼고


def test_make_client_picks_by_provider():
    assert isinstance(make_client(Backend(), Sampling()), LLMClient)
    assert isinstance(make_client(Backend(provider="anthropic", api_key="k", model="claude-opus-5-5",
                                          base_url="https://api.anthropic.com"), Sampling()), AnthropicClient)


# ───────────────────────── 연결 저장 · 고르기

@pytest.fixture
def conn_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONNECTIONS_FILE", tmp_path / "connections.yaml")
    monkeypatch.setattr(config, "SECRETS_FILE", tmp_path / "secrets.yaml")
    return tmp_path


def test_add_check_resolve_and_remove(conn_files, monkeypatch):
    found = [providers.ProviderModel(id=f"claude-m{i}", context_length=200_000) for i in range(5)]
    monkeypatch.setattr(providers, "_list_models", lambda p, key: [m.model_copy() for m in found])
    p = providers.add("anthropic", "회사 Claude", "sk-ant-secret-1234")
    assert p.id == "claude" and p.check_error == "" and [m.enabled for m in p.models] == [True, True, True, False, False]
    assert "sk-ant-secret-1234" not in (conn_files / "connections.yaml").read_text(encoding="utf-8")
    assert config.get_secret(p.secret_name) == "sk-ant-secret-1234" and p.key_hint() == "…1234"
    providers.update(p.id, models=[{"id": "claude-m0", "enabled": False}, {"id": "claude-m4", "enabled": True,
                                                                          "context_length": 50_000}])
    p = providers.refresh(p.id)   # 다시 확인해도 관리자가 바꾼 값은 유지
    assert {m.id: (m.enabled, m.context_length) for m in p.models}["claude-m4"] == (True, 50_000)
    assert [c["model"] for c in providers.choices()] == ["claude-m1", "claude-m2", "claude-m4"]
    b = providers.resolve(providers.choice_key(p.id, "claude-m4"))
    assert (b.provider, b.model, b.context_length, b.api_key, b.connection) == ("anthropic", "claude-m4", 50_000,
                                                                                 "sk-ant-secret-1234", p.id)
    with pytest.raises(ValueError, match="꺼 두었"):
        providers.resolve(providers.choice_key(p.id, "claude-m0"))
    with pytest.raises(ValueError, match="찾을 수 없"):
        providers.resolve("ext:nope:x")
    providers.remove(p.id)
    assert providers.all_providers() == [] and config.get_secret(p.secret_name) is None


def test_offline_switch_blocks_every_external_call(conn_files, monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "_list_models", lambda p, key: calls.append(1) or [providers.ProviderModel(id="m")])
    p = providers.add("openai", "", "sk-1", check=False)
    access = accounts.load_access()
    access.offline = True
    accounts.save_access(access)
    assert providers.refresh(p.id).check_error.startswith("폐쇄망") and not calls
    assert providers.choices() == []
    with pytest.raises(ValueError, match="폐쇄망"):
        providers.resolve(providers.choice_key(p.id, "m"))


def test_check_failure_is_explained(conn_files, monkeypatch):
    def boom(p, key):
        raise anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=_req()), body=None)
    monkeypatch.setattr(providers, "_list_models", boom)
    p = providers.add("anthropic", "", "bad")
    assert "401" in p.check_error and "키" in p.check_error


def test_claude_account_login_uses_a_profile_not_a_key(conn_files, monkeypatch):
    """회사 Claude 계정 로그인: 키 없이 프로필 이름만 저장하고, 모델 목록·실행은 그 프로필로 공식 SDK 를 부른다."""
    seen = {}

    class FakeAnthropic:
        def __init__(self, **kw):
            seen.update(kw)
            self.models = SimpleNamespace(list=lambda limit: [SimpleNamespace(id="claude-opus-5-5", display_name="Opus",
                                                                              max_input_tokens=1_000_000)])

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    p = providers.add("anthropic", "회사 Claude 계정", "", auth="account_login", profile="corp")
    assert p.auth == "account_login" and p.profile == "corp" and p.check_error == ""
    assert seen.get("profile") == "corp" and "api_key" not in seen                     # 키 대신 프로필
    assert not (conn_files / "secrets.yaml").exists()                                  # 비밀 저장소에 아무것도 안 남김
    assert p.key_hint() == "계정 로그인 (프로필 corp)" and "auth login --profile corp" in providers.login_command(p)
    b = providers.resolve(providers.choice_key(p.id, "claude-opus-5-5"))
    assert (b.profile, b.api_key, b.context_length) == ("corp", "", 1_000_000)
    seen.clear()
    AnthropicClient(b, Sampling())
    assert seen.get("profile") == "corp"
    with pytest.raises(ValueError, match="프로필 이름"):
        providers.add("anthropic", "", "", auth="account_login", profile="bad name!")


@pytest.mark.parametrize("kind,why", [("openai", "승인"), ("gemini", "약관")])
def test_account_login_is_refused_where_providers_forbid_it(conn_files, kind, why):
    with pytest.raises(ValueError, match=why):
        providers.add(kind, "", "", auth="account_login")


def test_expired_claude_login_is_explained():
    err = type("CredentialsError", (Exception,), {})("profile 'corp' not found")
    assert "Claude 계정으로 로그인" in providers.describe_error(err)


@pytest.mark.parametrize("kind,asked,sent", [("anthropic", "max", "max"), ("anthropic", "xhigh", "xhigh"),
                                              ("openai", "max", "high"), ("gemini", "xhigh", "high"), ("openai", "low", "low"),
                                              ("openai", "nonsense", "")])
def test_effort_is_mapped_per_provider(kind, asked, sent):
    assert providers.effort_for(kind, asked) == sent


@pytest.mark.parametrize("name,expect", [("APITimeoutError", "시간 초과"), ("APIConnectionError", "인터넷")])
def test_describe_error_network(name, expect):
    err = type(name, (Exception,), {})("x")
    assert expect in providers.describe_error(err)


# ───────────────────────── Claude 계정 로그인을 화면에서 (서버가 로그인 도구를 대신 띄운다 — 여기서는 가짜 도구)

FAKE_ANT = '''
import sys
manual = "--no-browser" in sys.argv
if manual:
    print("Open this URL to log in: https://platform.claude.com/oauth/authorize?state=x", flush=True)
    code = sys.stdin.readline().strip()
    if code != "good-code":
        print("invalid code", flush=True)
        sys.exit(1)
else:
    print("Opening your browser...", flush=True)
print("Logged in. Profile saved.", flush=True)
'''


def _fake_ant(monkeypatch, tmp_path):
    import sys
    from agent import claude_login
    script = tmp_path / "fake_ant.py"
    script.write_text(FAKE_ANT, encoding="utf-8")
    monkeypatch.setattr(claude_login, "argv", lambda profile, manual: [
        sys.executable, str(script), "auth", "login", "--profile", profile, *(["--no-browser"] if manual else [])])
    monkeypatch.setattr(claude_login, "tool_ready", lambda: True)
    return claude_login


def _until(fn, timeout=10):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if (v := fn()):
            return v
        time.sleep(0.05)
    raise AssertionError("기다려도 안 됨")


def test_claude_login_runs_from_the_screen(monkeypatch, tmp_path):
    login = _fake_ant(monkeypatch, tmp_path)
    done = []
    view = login.start("remote", "corp", manual=True, on_done=lambda: done.append("refreshed"))
    assert view["mode"] == "manual" and view["state"] == "running"
    url = _until(lambda: (login.status("remote") or {}).get("urls"))[0]
    assert url.startswith("https://platform.claude.com/oauth/")
    login.send("remote", "good-code")
    assert _until(lambda: login.status("remote")["state"] == "done" and done) == ["refreshed"]
    assert login.start("local", "corp", manual=False)["mode"] == "browser"   # 서버 PC: 브라우저가 열리고 스스로 끝난다
    _until(lambda: login.status("local")["state"] == "done")
    login.start("bad", "corp", manual=True)
    _until(lambda: login.status("bad")["urls"])
    login.send("bad", "wrong")
    _until(lambda: login.status("bad")["state"] == "failed")
    assert "종료 코드 1" in login.status("bad")["message"]
    with pytest.raises(ValueError, match="진행 중인 로그인이 없습니다"):
        login.send("bad", "again")


def test_claude_login_api_and_refresh(conn_files, monkeypatch, tmp_path):
    import server.app as srv
    from fastapi.testclient import TestClient
    from tests.test_multiuser import H
    _fake_ant(monkeypatch, tmp_path)
    monkeypatch.setattr(providers, "_list_models", lambda p, key: [providers.ProviderModel(id="claude-opus-5-5")])
    p = providers.add("anthropic", "회사 Claude", "", auth="account_login", profile="corp", check=False)
    c = TestClient(srv.app, base_url="http://127.0.0.1")                       # 서버 PC 에서 누름
    st = c.post(f"/api/v1/admin/providers/{p.id}/login", headers=H).json()
    assert st["mode"] == "browser"
    _until(lambda: c.get(f"/api/v1/admin/providers/{p.id}/login").json()["state"] == "done")
    _until(lambda: [m.id for m in providers.get(p.id).models] == ["claude-opus-5-5"])   # 로그인하면 모델 목록을 받는다
    remote = TestClient(srv.app, base_url="http://127.0.0.1", client=("10.0.0.7", 5000))
    assert remote.post(f"/api/v1/admin/providers/{p.id}/login", headers=H).json()["mode"] == "manual"
    key = providers.add("openai", "", "sk-x", check=False)
    assert c.post(f"/api/v1/admin/providers/{key.id}/login", headers=H).status_code == 400   # API 키 연결은 로그인 없음


# ───────────────────────── 잔액 부족: 원문 영어 오류 대신 할 일을, '연결 확인'에서 미리

def test_credit_balance_error_is_explained():
    err = anthropic.BadRequestError("Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing",
                                    response=httpx2.Response(400, request=_req()), body=None)
    client, _ = claude(replies=[err])
    with pytest.raises(ProviderAPIError) as info:
        client.chat([{"role": "user", "content": "q"}])
    text = providers.describe_error(info.value)
    assert "잔액" in text and "Plans & Billing" in text and "구독" in text
    quota = openai.RateLimitError("You exceeded your current quota", response=httpx2.Response(429, request=_req()),
                                  body={"error": {"code": "insufficient_quota"}})
    assert providers.describe_error(quota) == providers.BILLING_NOTE


def test_connection_check_catches_empty_balance(conn_files, monkeypatch):
    monkeypatch.setattr(providers, "_list_models", lambda p, key: [providers.ProviderModel(id="claude-sonnet-5-5")])

    def broke(p, key, model):
        raise anthropic.BadRequestError("Your credit balance is too low", response=httpx2.Response(400, request=_req()), body=None)

    monkeypatch.setattr(providers, "_probe", broke)
    p = providers.add("anthropic", "", "sk-ant-x")
    assert p.check_error == providers.BILLING_NOTE and [m.id for m in p.models] == ["claude-sonnet-5-5"]

    def odd(p, key, model):   # 요청 모양 문제 같은 다른 오류는 연결 실패로 보지 않는다
        raise anthropic.BadRequestError("max_tokens too small", response=httpx2.Response(400, request=_req()), body=None)

    monkeypatch.setattr(providers, "_probe", odd)
    assert providers.refresh(p.id).check_error == ""
