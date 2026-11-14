"""하네스 (agent/harness.py · agent/tools/knowledge.py · server/api/harness.py):
관리자가 마크다운으로 관리하는 공통 지침 · 역할 · 자료(사용설명서 · 에러 대처) — 회사별 대상, 찾기·읽기, 역할 파일, 작업 폴더 지침."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from agent import harness
from agent.loop import Agent
from agent.profiles import list_profiles, load_profile
from agent.tools import REGISTRY, ToolContext
from agent.safety import Sandbox
from tests.conftest import FakeLLM

MANUAL = """---
title: X100 사용설명서
description: 코팅기 X100 에러 대처
tags: [X100, 에러]
---

# X100 사용설명서

## E-104 노즐 과열
1. 전원을 끄고 10분 식힌다.
2. 노즐 필터를 꺼내 세척한다.

## E-221 압력 부족
에어 공급 밸브를 확인한다.
"""


@pytest.fixture
def tree():
    h = harness.root()
    (h / "agents").mkdir(parents=True)
    (h / "AGENT.md").write_text("모든 답은 존댓말로 한다.\n", encoding="utf-8")
    (h / "A사" / "X100").mkdir(parents=True)
    (h / "A사" / "AGENT.md").write_text("---\ncompanies: [A사]\n---\nA사 장비 문의는 자료를 먼저 찾는다.\n", encoding="utf-8")
    (h / "A사" / "X100" / "manual.md").write_text(MANUAL, encoding="utf-8")
    (h / "B사").mkdir()
    (h / "B사" / "AGENT.md").write_text("---\ncompanies: [B사]\n---\nB사 전용 지침\n", encoding="utf-8")
    (h / "B사" / "secret.md").write_text("# B사 단가표\n단가 1,000원\n", encoding="utf-8")
    (h / "공통").mkdir()
    (h / "공통" / "안전수칙.md").write_text("# 안전수칙\n보호구를 착용한다.\n", encoding="utf-8")
    return h


def test_front_matter_roundtrip_and_errors():
    meta, body = harness.split_front("---\ntitle: 가\ntags: [a]\n---\n\n본문\n")
    assert meta == {"title": "가", "tags": ["a"]} and body.strip() == "본문"
    assert harness.split_front("본문만")[0] == {}
    assert harness.split_front(harness.join_front({"name": "x"}, "본문"))[0] == {"name": "x"}
    assert "닫히지" in harness.check("---\ntitle: 가\n본문")
    assert "yaml" in harness.check("---\ntitle: [\n---\n")


def test_company_scope_is_inherited_and_filters_everything(tree):
    folders = {f.path: f for f in harness.scan()[0]}
    assert folders["A사/X100"].effective == ["A사"] and folders["공통"].effective == []
    a = {d.path for d in harness.visible_docs("A사")}
    assert "A사/X100/manual.md" in a and "공통/안전수칙.md" in a and "B사/secret.md" not in a
    assert {d.path for d in harness.visible_docs(None)} >= {"B사/secret.md", "A사/X100/manual.md"}   # 관리자 · 혼자 쓰기
    assert "B사/secret.md" not in {d.path for d in harness.visible_docs("")}                         # 회사 없는 사용자
    text = harness.instructions("A사", 5000)
    assert "존댓말" in text and "A사 장비 문의" in text and "B사 전용" not in text
    assert "manual.md — X100 사용설명서: 코팅기 X100 에러 대처" in harness.catalog("A사", 30)
    assert "secret" not in harness.catalog("A사", 30)


def test_search_and_read_in_pieces(tree):
    hits = harness.search("A사", "E-104 과열")
    assert hits[0]["path"] == "A사/X100/manual.md" and hits[0]["section"] == "E-104 노즐 과열"
    assert "식힌다" not in hits[0]["snippet"] or "과열" in hits[0]["snippet"]
    assert harness.search("A사", "단가") == []                                    # B사 자료는 A사에서 안 보인다
    text, nxt, total = harness.read("A사", "A사/X100/manual.md", 0, 40)
    assert nxt == 40 and total > 40 and "X100" in text
    with pytest.raises(ValueError):
        harness.read("A사", "B사/secret.md")
    with pytest.raises(ValueError):
        harness.read(None, "../config/secrets.md")


def test_knowledge_tools_follow_the_users_company(tree, settings, tmp_path):
    settings.audience = "B사"
    ctx = ToolContext(settings, Sandbox.from_settings(settings), tmp_path / "run")
    assert "B사/secret.md" in REGISTRY["search_knowledge"].call(ctx, {"query": "단가"})
    assert "없습니다" in REGISTRY["search_knowledge"].call(ctx, {"query": "E-104"})
    assert "단가 1,000원" in REGISTRY["read_knowledge"].call(ctx, {"path": "B사/secret.md"})


def test_agent_gets_instructions_catalog_and_tools(tree, settings, profile):
    settings.audience = "A사"
    agent = Agent(settings, profile, llm=FakeLLM([]))
    prompt = agent.system_prompt()
    assert "존댓말" in prompt and "A사 장비 문의" in prompt and "manual.md" in prompt and "B사" not in prompt
    assert {"search_knowledge", "read_knowledge"} <= set(agent.tools)


def test_no_knowledge_means_no_extra_tools(settings, profile):
    agent = Agent(settings, profile, llm=FakeLLM([]))
    assert "search_knowledge" not in agent.tools and "하네스" not in agent.system_prompt()


def test_project_instruction_files_in_the_work_folder(settings, profile, ws):
    (ws / "CLAUDE.md").write_text("이 프로젝트는 pytest 로 시험한다.\n", encoding="utf-8")
    sub = ws / "proj"
    sub.mkdir()
    (sub / "AGENT.md").write_text("---\ntitle: x\n---\n하위 폴더 규칙: 한국어 주석\n", encoding="utf-8")
    text = harness.project_instructions(sub, ws, 5000)
    assert text.index("pytest") < text.index("한국어 주석")                     # 위 폴더 것 먼저, 가까운 것이 나중
    agent = Agent(settings, profile, llm=FakeLLM([]), workdir=sub)
    assert "한국어 주석" in agent.system_prompt()


def test_role_files_in_claude_code_style(tree):
    (tree / "agents" / "support.md").write_text(
        "---\nname: support\ndescription: 장비 문의 담당\ntools: Read, Grep, Bash, Mystery\nmodel: sonnet\n---\n"
        "너는 고객사 장비 문의에 답하는 담당자다.\n", encoding="utf-8")
    assert "support" in list_profiles()
    p = load_profile("support")
    assert p.tools == ["read_file", "search_files", "run_command"] and p.description == "장비 문의 담당"
    assert p.system_prompt.startswith("너는 고객사")
    with pytest.raises(ValueError, match="모르는 도구"):   # 저장할 때는 모르는 도구 이름을 알려 준다
        harness.write("agents/support.md", (tree / "agents" / "support.md").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="본문"):
        harness.write("agents/empty.md", "---\nname: empty\n---\n")


def test_md_role_overrides_yaml_and_export(tree):
    from agent.profiles import Profile
    md = harness.role_markdown(Profile(name="general", description="d", system_prompt="역할 지침\n", tools=["read_file"],
                                       think=False))
    harness.write("agents/general.md", md)
    p = load_profile("general")
    assert p.tools == ["read_file"] and p.think is False and p.system_prompt.strip() == "역할 지침"


def test_write_paths_are_confined(tree):
    for bad in ("../x.md", "a/../../x.md", "x.txt", ".hidden/x.md", "C:/x.md"):
        with pytest.raises(ValueError):
            harness.write(bad, "# x")
    with pytest.raises(ValueError):
        harness.delete("")
    harness.mkdir("C사/장비")
    harness.write("C사/장비/a.md", "# A")
    harness.rename("C사/장비/a.md", "C사/장비/b.md")
    assert (tree / "C사" / "장비" / "b.md").is_file()
    harness.delete("C사")
    assert not (tree / "C사").exists()


def test_set_companies_keeps_the_folder_instructions(tree):
    f = harness.set_companies("A사", ["A사", "D사"])
    assert f.companies == ["A사", "D사"]
    assert "A사 장비 문의" in (tree / "A사" / "AGENT.md").read_text(encoding="utf-8")
    assert harness.set_companies("A사", []).effective == []


def test_import_converts_files_to_markdown(tree):
    doc = harness.import_file("공통", "점검표.csv", "항목,주기\n필터,매주\n".encode("utf-8"))
    assert doc.path == "공통/점검표.md" and doc.title == "점검표"
    assert "필터" in (tree / "공통" / "점검표.md").read_text(encoding="utf-8")
    assert harness.import_file("공통", "note.txt", "그대로".encode("utf-8")).path == "공통/note.md"


# ───────────────────────── 관리 API

def test_harness_api_is_admin_only_and_works(tree, monkeypatch):
    import server.app as srv
    from server import deps
    from tests.test_multiuser import H, issue
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    t = c.get("/api/v1/harness").json()
    assert {"A사/X100/manual.md", "AGENT.md"} <= {f["path"] for f in t["files"]}
    assert c.put("/api/v1/harness/file", json={"path": "공통/새.md", "text": "# 새 자료\n내용"}, headers=H).status_code == 200
    assert c.get("/api/v1/harness/file", params={"path": "공통/새.md"}).json()["text"].startswith("# 새 자료")
    assert c.put("/api/v1/harness/file", json={"path": "../x.md", "text": "x"}, headers=H).status_code == 400
    up = c.post("/api/v1/harness/upload", json={"folder": "공통", "files": [
        {"name": "faq.txt", "data": base64.b64encode("자주 묻는 질문".encode()).decode()},
        {"name": "bad.bin", "data": "!!!"}]}, headers=H).json()
    assert [d["path"] for d in up["saved"]] == ["공통/faq.md"] and len(up["failed"]) == 1
    assert c.put("/api/v1/harness/folders/companies", json={"path": "공통", "companies": ["A사"]},
                 headers=H).json()["effective"] == ["A사"]
    exported = c.post("/api/v1/harness/export-roles", headers=H).json()["written"]
    assert "agents/general.md" in exported and load_profile("general").name == "general"
    # 로그인 사용 모드의 일반 사용자는 하네스를 못 고친다
    admin = TestClient(srv.app, base_url="http://127.0.0.1")
    admin.post("/api/v1/auth/setup", json={"username": "boss", "password": "Adm1nPass!"}, headers=H)
    user = issue(admin, "h_user")
    assert user.get("/api/v1/harness").status_code == 403
    assert admin.get("/api/v1/harness").status_code == 200
    _ = deps


def test_settings_screen_marks_harness_roles(tree):
    import server.app as srv
    (tree / "agents" / "support.md").write_text("---\nname: support\ntools: [read_file]\n---\n장비 문의 담당\n", encoding="utf-8")
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    entries = {p["name"]: p for p in c.get("/api/v1/settings").json()["profiles"]}
    assert entries["support"]["harness"] and entries["support"]["file"] == "harness/agents/support.md"
    assert not entries["general"]["harness"] and entries["general"]["file"] == "config/agents/general.yaml"


def test_preview_does_not_flag_auto_knowledge_tools(tree, settings, profile):
    """하네스 자료가 있으면 search_knowledge · read_knowledge 는 어느 용도든 저절로 붙는다 — 미리보기 점검이 '없는 도구'라고 하지 않는다."""
    from agent import prompt_preview, prompts
    agent = Agent(settings, profile, llm=FakeLLM([]))
    assert "search_knowledge" not in profile.tools and "search_knowledge" in agent.tools
    warnings = [w["text"] for w in prompt_preview.lint(agent, profile, prompts.load_prompts())]
    assert not any("없는 도구" in w and "knowledge" in w for w in warnings)
