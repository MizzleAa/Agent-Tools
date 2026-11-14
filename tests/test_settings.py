"""설정 화면: 프롬프트 조립(블록·변수·조건 줄), 바꾼 프롬프트 적용, 변경 기록, settings.yaml 값 고치기, 설정 API.
실제 config 폴더는 건드리지 않는다 (임시 폴더에 복사해서 쓴다)."""

import os
import shutil
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import server.app as srv
from agent import workflow as wfmod
from server import deps
from agent import config, history, profiles, prompts, settings_file, standards
from agent.config import Sampling, load_settings
from agent.events import Note, ToolResult
from agent.llm import LLMClient
from agent.loop import Agent, profile_sampling
from agent.profiles import Profile, load_profile
from tests.conftest import FakeLLM, call, reply

REAL_CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """진짜 config 를 임시 폴더에 복사하고 모든 경로를 그쪽으로 돌린다."""
    root = tmp_path / "config"
    shutil.copytree(REAL_CONFIG, root, ignore=shutil.ignore_patterns("secrets.yaml", "connections.yaml", "prompts.yaml"))
    monkeypatch.setattr(config, "CONFIG_DIR", root)
    monkeypatch.setattr(config, "CONNECTIONS_FILE", root / "connections.yaml")
    monkeypatch.setattr(config, "SECRETS_FILE", root / "secrets.yaml")
    monkeypatch.setattr(profiles, "AGENTS_DIR", root / "agents")
    monkeypatch.setattr(prompts, "USER_FILE", root / "prompts.yaml")
    monkeypatch.setattr(standards, "STANDARDS_DIR", root / "standards")
    data = tmp_path / "data"
    monkeypatch.setattr(wfmod, "WORKFLOWS_DIR", root / "workflows")
    monkeypatch.setattr(deps, "load_settings", lambda: load_settings(root / "settings.yaml").model_copy(update={"data_dir": str(data)}))
    return root


@pytest.fixture
def client(cfg):
    return TestClient(srv.app, base_url="http://127.0.0.1", headers={"X-Agent-Token": deps.TOKEN})


# ───────────────────────── 조립 규칙

def test_render_vars_and_conditional_lines():
    text = "a {{x}}\n{{?run_command}}- 명령 줄\n{{?!run_command}}- 명령 없음\n{{?web_search|empty}}- 검색\n  {{?x}}  들여쓰기 유지\n{{모름}}"
    out = prompts.render(text, {"x": "값", "empty": ""}, {"run_command"})
    assert out.splitlines() == ["a 값", "- 명령 줄", "    들여쓰기 유지", "{{모름}}"]
    assert prompts.unknown_vars(text, {"x": "1"}) == {"모름"}


def test_default_prompts_keep_coder_sentences_and_drop_unused_tool_rules(settings):
    coder = Agent(settings, load_profile("coder"), llm=FakeLLM([])).system_prompt()
    for line in ("## 작업 방식", "- run_command 는 매번 새 셸에서 실행된다", "## 개발 규격 (요약", "## 실행 환경",
                 "셸: Windows PowerShell", "사용자가 쓴 언어로 답한다."):
        assert line in coder
    assert coder.index("## 개발 규격") < coder.index("## 실행 환경")   # 바뀌지 않는 것 앞, 바뀌는 것(폴더·날짜) 뒤
    general = Agent(settings, load_profile("general"), llm=FakeLLM([])).system_prompt()
    assert "run_command" not in general and "- OS: Windows 11." in general and "{{" not in general


def test_user_overrides_apply_to_next_run(settings, profile):
    data = prompts.apply_change({}, "messages", "nudge_act", "지금 바로 도구를 쓰세요. ({{answer_language}})")
    data = prompts.apply_change(data, "tools", "read_file", "파일 읽기. 오늘은 {{today}}")
    data = prompts.apply_change(data, "variables", "answer_language", {"value": "한국어"})
    data = prompts.apply_change(data, "blocks", "house_rules", {"title": "사내 규칙", "text": "## 사내 규칙\n- {{company}} 규칙을 따른다\n"})
    prompts.USER_FILE.write_text(prompts.user_text(data), encoding="utf-8")
    profile.blocks = ["house_rules", "work_rules"]
    profile.variables = {"company": "하이비젼"}
    llm = FakeLLM([reply(content="파일을 확인해서 작성하겠습니다."), reply(content="네")])
    agent = Agent(settings, profile, llm=llm)
    list(agent.run("정리"))
    system = llm.calls[0]["messages"][0]["content"]
    assert system.index("## 사내 규칙") < system.index("## 작업 방식") and "하이비젼 규칙" in system and "한국어로 답한다" in system
    assert "## 실행 환경" not in system                                   # 구성에서 뺀 블록은 들어가지 않는다
    assert llm.calls[1]["messages"][-1]["content"] == "지금 바로 도구를 쓰세요. (한국어)"
    desc = next(t for t in llm.calls[0]["tools"] if t["function"]["name"] == "read_file")["function"]["description"]
    assert desc.startswith("파일 읽기. 오늘은 20")


def test_same_as_default_is_not_stored_and_none_resets():
    default = prompts.load_prompts().messages["denied"].text
    assert prompts.apply_change({}, "messages", "denied", default) == {"messages": {}}
    data = prompts.apply_change({}, "messages", "denied", "거부됨")
    assert data["messages"] == {"denied": "거부됨"}
    assert prompts.apply_change(data, "messages", "denied", None)["messages"] == {}
    for section, key, value in [("messages", "nope", "x"), ("tools", "nope", "x"), ("blocks", "Bad-Name", {"text": "x"}),
                                ("variables", "cwd", {"value": "x"}), ("blocks", "lang_standard", {"text": "x"})]:
        with pytest.raises(ValueError):
            prompts.apply_change({}, section, key, value)


def test_loop_messages_come_from_store(settings, profile, ws):
    data = prompts.apply_change({}, "messages", "repeat_warning", "또 같은 호출")
    data = prompts.apply_change(data, "messages", "unknown_tool", "없는 도구 {{name}}")
    prompts.USER_FILE.write_text(prompts.user_text(data), encoding="utf-8")
    (ws / "a.txt").write_text("x", encoding="utf-8")
    llm = FakeLLM([reply(call("read_file", path="a.txt")), reply(call("read_file", path="a.txt")),
                   reply(call("nope_tool")), reply(content="끝")])
    results = [e for e in Agent(settings, profile, llm=llm).run("읽기") if isinstance(e, ToolResult)]
    assert results[1].output.endswith("또 같은 호출") and results[2].output == "없는 도구 nope_tool"


def test_profile_sampling_and_llm_extra_params():
    base = Sampling(top_k=20, min_p=0.0, presence_penalty=1.0, max_tokens=512)
    p = Profile(name="x", system_prompt="x", tools=[], temperature=0.3)
    s = profile_sampling(base, p)
    assert s.temperature == 0.3 and s.top_p == 0.95 and base.temperature == 0.6
    llm = LLMClient(config.Backend(), s)
    sent = {}

    class Resp:
        usage = None
        choices = [type("C", (), {"finish_reason": "stop", "message": type("M", (), {
            "content": "ok", "tool_calls": None, "model_extra": {}})()})()]

    llm.client.chat.completions.create = lambda **kw: sent.update(kw) or Resp()
    llm.chat([{"role": "user", "content": "hi"}], think=False)
    assert sent["temperature"] == 0.3 and sent["presence_penalty"] == 1.0 and sent["max_tokens"] == 512
    assert sent["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}, "top_k": 20, "min_p": 0.0}


def test_profile_yaml_round_trip_is_unchanged(cfg):
    for name in profiles.list_profiles():
        assert load_profile(name).to_yaml() == (cfg / "agents" / f"{name}.yaml").read_text(encoding="utf-8")
        assert (prompts.DEFAULT_AGENTS_DIR / f"{name}.yaml").read_text(encoding="utf-8") == load_profile(name).to_yaml()


def test_old_profile_without_blocks_gets_default_order():
    p = Profile(name="old", system_prompt="x", tools=["list_dir"], standards=True)
    assert p.effective_blocks() == ["work_rules", "dev_standard", "intranet_status", "environment", "lang_standard"]


# ───────────────────────── 변경 기록

def test_history_backup_restore_and_line_endings(cfg, tmp_path):
    data = tmp_path / "data"
    target = cfg / "settings.yaml"
    target.write_bytes(b"a: 1\r\nb: 2\r\n")
    entry = history.write(target, "a: 1\nb: 3\n", data, note="t")
    assert target.read_bytes() == b"a: 1\r\nb: 3\r\n"   # 원래 CRLF 면 CRLF 그대로
    assert history.write(target, "a: 1\nb: 3\n", data) == ""   # 바뀐 게 없으면 기록하지 않는다
    e = history.get(data, entry)
    assert e["text"] == "a: 1\nb: 2\n" and "-b: 2" in e["diff"] and "+b: 3" in e["diff"]
    history.restore(data, entry)
    assert target.read_bytes() == b"a: 1\r\nb: 2\r\n" and len(history.entries(data)) == 2
    new = cfg / "agents" / "brand-new.yaml"
    created = history.write(new, "x: 1\n", data)
    history.restore(data, created)
    assert not new.exists()   # 새로 만든 파일을 되돌리면 없던 상태로
    with pytest.raises(ValueError):
        history.write(cfg / "secrets.yaml", "x", data)
    with pytest.raises(ValueError):
        history.write(tmp_path / "outside.yaml", "x", data)
    with pytest.raises(ValueError):
        history.get(data, "../../x")


# ───────────────────────── settings.yaml 값 고치기

SAMPLE = """# 머리 주석
sampling:                 # 생성
  temperature: 0.6
  top_p: 0.95

web:
  search_provider: ddgs   # 검색 제공자
                          # 이어지는 설명
  regions: [kr-kr, us-en] # 지역
  allow_private_hosts: [] # 내부
                          # 예) wiki

limits:
  max_steps: 50           # 스텝
"""


def test_settings_file_keeps_comments_and_validates():
    out = settings_file.update(SAMPLE, "sampling", {"temperature": 0.7, "top_k": 20})
    out = settings_file.update(out, "web", {"regions": "kr-kr, wt-wt", "max_results": 5})
    out = settings_file.update(out, "limits", {"max_steps": 100})
    assert "# 머리 주석" in out and "sampling:                 # 생성" in out
    assert "  max_steps: 100          # 스텝" in out                      # 주석 열 위치 유지
    assert "  regions: [kr-kr, wt-wt] # 지역" in out
    assert out.index("max_results: 5") > out.index("# 예) wiki")            # 앞 키의 설명 주석 뒤에 넣는다
    raw = yaml.safe_load(out)
    assert raw["sampling"] == {"temperature": 0.7, "top_p": 0.95, "top_k": 20} and raw["web"]["regions"] == ["kr-kr", "wt-wt"]
    out = settings_file.update(out, "sampling", {"top_k": None})
    assert "top_k" not in out
    out = settings_file.update(out, "policy", {"block_downloads": False})   # 없는 구역은 끝에 만든다
    assert yaml.safe_load(out)["policy"] == {"block_downloads": False}
    for section, values in [("limits", {"max_steps": -1}), ("sampling", {"temperature": "x"}), ("web", {"search_provider": "bing"}),
                            ("limits", {"nope": 1}), ("runtime", {"port": 1}), ("policy", {"block_downloads": "yes"})]:
        with pytest.raises(ValueError):
            settings_file.update(SAMPLE, section, values)


def test_settings_file_on_real_settings_yaml():
    text = (REAL_CONFIG / "settings.yaml").read_text(encoding="utf-8")
    out = settings_file.update(text, "limits", {"command_timeout": 300})
    changed = [(a, b) for a, b in zip(text.splitlines(), out.splitlines()) if a != b]
    assert len(changed) == 1 and changed[0][1].startswith("  command_timeout: 300") and "# run_command" in changed[0][1]


# ───────────────────────── 설정 API (/api/v1/settings)

S = "/api/v1/settings"


def test_api_get_and_preview(client):
    d = client.get(S).json()
    assert {p["name"] for p in d["profiles"]} >= {"coder", "general", "intranet", "researcher", "reviewer"}
    assert all(p["builtin"] and p["modified"] is False and p["profile"]["name"] == p["name"] for p in d["profiles"])
    blocks = {b["id"]: b for b in d["blocks"]}
    assert blocks["intranet_status"]["dynamic"] and "coder" in blocks["dev_standard"]["used_by"]
    assert any(m["key"] == "nudge_act" for m in d["messages"]) and d["params"]["sections"]["sampling"]["fields"]
    assert {w["name"] for w in d["workflows"]} >= {"build-review", "research-report"}
    coder = next(p for p in d["profiles"] if p["name"] == "coder")["profile"]
    r = client.post(f"{S}/preview", json=coder).json()
    assert [s["id"] for s in r["sections"]] == ["role", "work_rules", "dev_standard", "environment"]
    assert r["warnings"] == [] and r["lang_extra"] and r["total_tokens"] > r["system_tokens"] > 0 and r["tools"]["json"]
    bad = {**coder, "blocks": ["dev_standard", "environment", "work_rules", "nope"], "tools": ["list_dir", "run_command"],
           "system_prompt": "web_search 로 찾고 {{oops}} 를 쓴다", "temperature": 0}
    warns = " / ".join(w["text"] for w in client.post(f"{S}/preview", json=bad).json()["warnings"])
    for needle in ("없는 블록", "{{oops}}", "언어별 규격", "web_search", "read_standard", "실행 환경", "온도"):
        assert needle in warns, needle


def test_api_profile_create_save_reset_delete(client, cfg):
    base = client.get(S).json()["profiles"][0]["profile"]
    new = {**base, "name": "writer", "description": "보고서", "tools": ["read_file", "write_file"], "variables": {"tone": "존댓말"}}
    r = client.post(f"{S}/profiles", json=new)
    assert r.status_code == 201 and r.json()["profile"]["variables"] == {"tone": "존댓말"} and not r.json()["builtin"]
    assert client.post(f"{S}/profiles", json=new).status_code == 409
    assert load_profile("writer").variables == {"tone": "존댓말"}
    assert "writer" in [p["name"] for p in client.get("/api/v1/profiles").json()]
    for broken in ({**new, "tools": ["nope"]}, {**new, "system_prompt": " "}, {**new, "variables": {"cwd": "x"}},
                   {**new, "tools": ["read_file"] * 2}, {**new, "top_p": 5}):
        r = client.put(f"{S}/profiles/writer", json=broken)
        assert r.status_code == 400 and isinstance(r.json()["detail"], str), broken
    assert client.post(f"{S}/profiles", json={**new, "name": "Bad Name"}).status_code == 400
    assert client.put(f"{S}/profiles/other", json=new).status_code == 400     # 주소와 이름이 다름
    assert client.put(f"{S}/profiles/ghost", json={**new, "name": "ghost"}).status_code == 404
    coder = load_profile("coder").model_dump()
    coder["temperature"] = 0.4
    r = client.put(f"{S}/profiles/coder", json=coder).json()
    assert r["modified"] is True and load_profile("coder").temperature == 0.4
    assert client.delete(f"{S}/profiles/coder").status_code == 400            # 기본 제공은 못 지움
    assert client.post(f"{S}/profiles/coder/reset").json()["modified"] is False
    assert client.delete(f"{S}/profiles/writer").status_code == 200
    assert not (cfg / "agents" / "writer.yaml").exists()
    notes = [e["note"] for e in client.get(f"{S}/history").json()]
    assert notes[:4] == ["프로필 삭제 writer", "프로필 기본값으로 coder", "프로필 저장 coder", "새 프로필 writer"]


def test_api_prompts_params_standards_and_restore(client, cfg):
    assert client.put(f"{S}/messages/denied", json={"text": "거부됨"}).status_code == 200
    assert prompts.load_prompts().messages["denied"].text == "거부됨"
    assert client.put(f"{S}/messages/nope", json={"text": "x"}).status_code == 400
    assert client.put(f"{S}/messages/denied", json={"text": ""}).status_code == 400
    assert client.delete(f"{S}/messages/denied").status_code == 200
    assert not prompts.load_prompts().messages["denied"].modified
    assert client.put(f"{S}/tools/read_file", json={"text": "파일 읽기"}).status_code == 200
    assert prompts.load_prompts().tools["read_file"] == "파일 읽기"
    assert client.put(f"{S}/variables/company", json={"value": "하이비젼"}).status_code == 200
    assert client.put(f"{S}/variables/cwd", json={"value": "x"}).status_code == 400
    assert client.put(f"{S}/blocks/mine", json={"title": "내 블록", "text": "## 내 것\n"}).status_code == 200
    coder = load_profile("coder").model_dump()
    coder["blocks"] = ["mine", *coder["blocks"]]
    client.put(f"{S}/profiles/coder", json=coder)
    r = client.delete(f"{S}/blocks/mine")
    assert r.status_code == 400 and "coder" in r.json()["detail"]   # 쓰는 프로필이 있으면 못 지운다

    r = client.put(f"{S}/params/sampling", json={"values": {"temperature": 0.7, "top_k": 20}})
    assert r.status_code == 200 and r.json()["file"]["top_k"] == 20
    assert "# Qwen 권장값" in (cfg / "settings.yaml").read_text(encoding="utf-8")   # 주석 유지
    assert deps.load_settings().sampling.top_k == 20
    assert client.put(f"{S}/params/limits", json={"values": {"max_steps": -5}}).status_code == 400
    assert client.put(f"{S}/params/runtime", json={"values": {"port": 1}}).status_code == 404

    name = client.get(S).json()["standards"][0]["name"]
    text = client.get(f"{S}/standards/{name}").json()["text"]
    assert client.put(f"{S}/standards/{name}", json={"text": text + "\n추가 줄\n"}).status_code == 200
    assert client.get(f"{S}/standards/..%2Fsettings.yaml").status_code == 404

    items = client.get(f"{S}/history").json()
    param_entry = next(e for e in items if e["target"] == "settings.yaml")
    detail = client.get(f"{S}/history/{param_entry['id']}").json()
    assert "+  top_k: 20" in detail["diff"] and detail["existed"]
    assert client.post(f"{S}/history/{param_entry['id']}/restore").json()["target"] == "settings.yaml"
    assert deps.load_settings().sampling.top_k is None and deps.load_settings().sampling.temperature == 0.6
    assert client.get(f"{S}/history/nope").status_code == 404


def test_settings_change_needs_token(cfg):
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    assert c.put(f"{S}/messages/denied", json={"text": "x"}).status_code == 403


def test_gate_and_failure_messages_still_reach_model(settings, ws):
    """규격 점검·실패 경고 문구가 저장소에서 와도 예전처럼 모델에게 간다."""
    p = load_profile("coder")
    llm = FakeLLM([reply(call("write_file", path="main.py", content="print(1)")), reply(content="다 만들었습니다"),
                   reply(content="## 결과: 부분 완료")])
    events = list(Agent(settings, p, llm=llm, approver=lambda r: True, max_steps=0).run("만들어"))
    gate = next(e for e in events if isinstance(e, Note) and e.kind == "gate")
    assert gate.injected.startswith("최종 보고 전에 개발 규격으로 점검했더니") and "\n- " in gate.injected


# ───────────────────────── 모델 탭 (추가·값 바꾸기·기본값으로·빼기)

def test_model_name_suggestion_and_reserved_args():
    from agent import model_presets
    assert model_presets.suggest_name("Qwen3.6-35B-A3B-UD-Q4_K_M.gguf") == "qwen3.6-35b-a3b"
    assert model_presets.suggest_name("Qwen3-8B-Q4_K_M.gguf") == "qwen3-8b"
    assert model_presets.parse_args("--n-cpu-moe 36 --threads 8") == ["--n-cpu-moe", "36", "--threads", "8"]
    for bad in ("--host 0.0.0.0", "--port=9000", "-c 4096", "-m x.gguf"):
        with pytest.raises(ValueError):
            model_presets.parse_args(bad)


def test_api_models_add_override_reset_delete(client, cfg, tmp_path):
    gguf = tmp_path / "Tiny-1B-Q4_K_M.gguf"
    gguf.write_bytes(b"GGUF")
    os.utime(gguf, (time.time() - 60, time.time() - 60))   # 방금 바뀐 파일은 '내려받는 중'으로 보므로
    r = client.post(f"{S}/models", json={"name": "tiny-1b", "file": str(gguf), "note": "작은 모델", "context_length": 8192,
                                         "extra_args": "--threads 4"})
    assert r.status_code == 201, r.text
    s = deps.load_settings()
    tiny = next(m for m in s.models if m.name == "tiny-1b")
    assert tiny.managed and tiny.context_length == 8192 and tiny.extra_args == ["--threads", "4"]
    for bad in ({"name": "tiny-1b", "file": str(gguf)}, {"name": "x y", "file": str(gguf)},
                {"name": "nofile", "file": str(tmp_path / "none.gguf")}, {"name": "bad-args", "file": str(gguf), "extra_args": "--host 0.0.0.0"}):
        assert client.post(f"{S}/models", json=bad).status_code == 400

    # settings.yaml 에 있는 모델 값을 바꾸면 model_overrides 로 저장되고 실제 적용값이 바뀐다
    active = s.backend.model
    r = client.put(f"{S}/models/{active}", json={"context_length": 20480, "gpu_layers": 30, "extra_args": ""})
    assert r.status_code == 200 and r.json()["restart_needed"] is True and r.json()["model"]["overridden"]
    s = deps.load_settings()
    assert s.backend.context_length == 20480 and s.runtime.gpu_layers == 30
    conn = yaml.safe_load((cfg / "connections.yaml").read_text(encoding="utf-8"))
    assert conn["model_overrides"] == {active: {"context_length": 20480, "gpu_layers": 30}}   # 원래와 같은 값은 저장 안 함
    assert "# 에이전트 전역 설정" in (cfg / "settings.yaml").read_text(encoding="utf-8")       # settings.yaml 은 그대로
    models = {m["name"]: m for m in client.get(S).json()["models"]}
    assert models[active]["overridden"] and models[active]["active"] and models["tiny-1b"]["managed"]
    assert client.put(f"{S}/models/ghost", json={"note": "x"}).status_code == 400

    assert client.post(f"{S}/models/{active}/reset").json()["restart_needed"] is True
    assert "model_overrides" not in yaml.safe_load((cfg / "connections.yaml").read_text(encoding="utf-8"))
    assert client.delete(f"{S}/models/{active}").status_code == 400      # settings.yaml 모델
    assert client.delete(f"{S}/models/tiny-1b").status_code == 200
    assert all(m.name != "tiny-1b" for m in deps.load_settings().models)
    notes = [e["note"] for e in client.get(f"{S}/history").json()]
    assert notes[:4] == ["모델 삭제 tiny-1b", f"모델 기본값으로 {active}", f"모델 설정 {active}", "모델 추가 tiny-1b"]
