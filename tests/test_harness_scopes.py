"""하네스 여러 개 (공통 harness/ + 회사·프로젝트 harnesses/<이름>/): 실행에 합쳐 붙기 · 용도 찾는 순서 · 자료 경로 ·
워크플로 노드의 하네스 · 관리 API · 입력창 목록."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import harness
from agent.loop import Agent
from agent.profiles import list_profiles, load_profile
from agent.safety import Sandbox
from agent.tools import REGISTRY, ToolContext
from agent.workflow import Workflow, WorkflowRunner, lint
from tests.conftest import FakeLLM, reply

ROLE = "---\nname: support\ndescription: {desc}\ntools: [read_file]\n---\n{body}\n"


@pytest.fixture
def scopes():
    (harness.root() / "agents").mkdir(parents=True)
    (harness.root() / "AGENT.md").write_text("공통 규칙 C-1\n", encoding="utf-8")
    harness.create_scope("acme", "ACME 회사", "company", ["A사"])
    harness.write("AGENT.md", "---\ntitle: ACME 회사\nkind: company\ncompanies: [A사]\n---\nACME 규칙 Q-7\n", "acme")
    harness.write("agents/support.md", ROLE.format(desc="ACME 문의", body="너는 ACME 담당이다."), "acme")
    harness.write("장비/m.md", "---\ntitle: M 설명서\n---\n# M\n## E-900\n리셋 버튼을 5초 누른다.\n", "acme")
    harness.create_scope("proj-x", "프로젝트 X", "project")
    harness.write("AGENT.md", "---\ntitle: 프로젝트 X\nkind: project\n---\n프로젝트 X 규칙 PX-1\n", "proj-x")
    harness.write("agents/support.md", ROLE.format(desc="X 문의", body="너는 프로젝트 X 담당이다."), "proj-x")
    harness.write("agents/tester.md", "---\nname: tester\ntools: [read_file]\n---\n시험 담당\n", "proj-x")
    harness.write("문서/x.md", "# X 문서\nPX 배포 절차: 태그를 단다.\n", "proj-x")


def test_active_harnesses_and_role_lookup(scopes):
    assert [s.id for s in harness.list_scopes()] == ["acme", "proj-x"]
    assert harness.active("A사") == ["", "acme"]                       # 회사 하네스는 그 회사 사람에게 저절로
    assert harness.active("B사") == [""] and harness.active(None) == [""]
    assert harness.active("A사", "proj-x") == ["", "acme", "proj-x"]
    assert harness.active("B사", "acme") == [""]                       # 다른 회사 하네스는 골라도 못 쓴다
    assert harness.usable("proj-x", "B사") and not harness.usable("acme", "B사") and harness.usable("acme", None)
    # 같은 이름의 용도: 고른 하네스 → 회사 하네스 → 공통 순서
    assert "ACME 담당" in load_profile("support", scopes=["", "acme"]).system_prompt
    assert "프로젝트 X 담당" in load_profile("support", scopes=["", "acme", "proj-x"]).system_prompt
    with pytest.raises(FileNotFoundError):
        load_profile("tester")                                         # 공통에는 없다
    assert "tester" in list_profiles(scopes=["", "proj-x"]) and "tester" not in list_profiles()


def test_instructions_catalog_and_knowledge_merge(scopes, settings, profile, tmp_path):
    sc = harness.active("A사", "proj-x")
    text = harness.instructions("A사", 5000, sc)
    assert text.index("C-1") < text.index("Q-7") < text.index("PX-1") and "하네스 'ACME 회사' 지침" in text
    assert "@acme/장비/m.md — M 설명서" in harness.catalog("A사", 30, sc)
    hit = harness.search("A사", "E-900", scopes=sc)[0]
    assert hit["path"] == "@acme/장비/m.md"
    assert "리셋" in harness.read("A사", hit["path"], scopes=sc)[0]
    with pytest.raises(ValueError):
        harness.read("A사", "@proj-x/문서/x.md", scopes=["", "acme"])    # 이 실행에 붙지 않은 하네스
    # 실행: Settings.harness 로 고른 하네스가 지침·자료·도구에 붙는다
    settings.audience, settings.harness = "B사", "proj-x"
    agent = Agent(settings, profile, llm=FakeLLM([]))
    prompt = agent.system_prompt()
    assert "PX-1" in prompt and "Q-7" not in prompt and "@proj-x/문서/x.md" in prompt
    ctx = ToolContext(settings, Sandbox.from_settings(settings), tmp_path / "run")
    assert "@proj-x/문서/x.md" in REGISTRY["search_knowledge"].call(ctx, {"query": "배포"})
    assert "태그를 단다" in REGISTRY["read_knowledge"].call(ctx, {"path": "@proj-x/문서/x.md"})
    with pytest.raises(Exception, match="없습니다"):
        REGISTRY["read_knowledge"].call(ctx, {"path": "@acme/장비/m.md"})       # B사 사람에게 ACME 하네스는 안 붙는다


def test_workflow_node_picks_harness_and_role(scopes, settings):
    w = Workflow.model_validate({"name": "t", "nodes": [
        {"id": "a", "harness": "proj-x", "profile": "tester", "prompt": "{{input}}"},
        {"id": "b", "profile": "general", "prompt": "{{a}}"}]})
    assert w.summary()[0]["harness"] == "proj-x" and "harness" not in w.to_yaml().split("- id: b")[1]
    assert lint(w) == []
    llm = FakeLLM([reply(content="A 끝"), reply(content="B 끝")])
    events = list(WorkflowRunner(settings, w, llm=llm).run("일"))
    assert events[-1].status == "done"
    first, second = llm.calls[0]["messages"][0]["content"], llm.calls[1]["messages"][0]["content"]
    assert "시험 담당" in first and "PX-1" in first and "PX-1" not in second   # 노드에서 고른 하네스는 그 노드에만
    bad = Workflow.model_validate({"name": "t", "nodes": [{"id": "a", "harness": "gone", "profile": "general", "prompt": "x"}]})
    assert "없습니다" in lint(bad)[0]
    with pytest.raises(FileNotFoundError):
        WorkflowRunner(settings, bad)
    missing = Workflow.model_validate({"name": "t", "nodes": [{"id": "a", "profile": "tester", "prompt": "{{input}}"}]})
    assert "tester" in lint(missing)[0]


def test_cli_instructions_follow_chosen_harness(scopes, tmp_path):
    from agent.claude_code import instructions
    r = instructions.build("tester", tmp_path, None, "proj-x")
    assert r["profile"] == "tester" and r["harness"] == "proj-x"
    assert "C-1" in r["text"] and "PX-1" in r["text"] and "시험 담당" in r["text"] and "하네스: 프로젝트 X" in r["text"]
    assert "@proj-x/문서/x.md" in r["text"] and str(harness.root("proj-x")) in r["dirs"]
    assert {x["name"]: x["description"] for x in r["roles"]}["support"] == "X 문의"     # 같은 이름은 고른 하네스 것
    plain = instructions.build("tester", tmp_path)                                    # 고르지 않으면 공통만 → tester 없음
    assert plain["profile"] != "tester" and "PX-1" not in plain["text"] and plain["harness"] == ""


def test_scope_api_and_composer_list(scopes):
    import server.app as srv
    from tests.test_multiuser import H
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    assert [s["id"] for s in c.get("/api/v1/harness/scopes").json()] == ["acme", "proj-x"]
    made = c.post("/api/v1/harness/scopes", json={"id": "beta", "title": "베타", "kind": "project"}, headers=H)
    assert made.status_code == 200 and made.json()["title"] == "베타"
    assert c.post("/api/v1/harness/scopes", json={"id": "Bad Name"}, headers=H).status_code == 400
    assert c.post("/api/v1/harness/scopes", json={"id": "beta"}, headers=H).status_code == 400   # 이미 있음
    assert c.put("/api/v1/harness/file", params={"scope": "beta"}, json={"path": "agents/ops.md",
                 "text": "---\nname: ops\ntools: [read_file]\n---\n운영 담당\n"}, headers=H).status_code == 200
    t = c.get("/api/v1/harness", params={"scope": "beta"}).json()
    assert t["scope"] == "beta" and "agents/ops.md" in {f["path"] for f in t["files"]} and t["yaml_roles"] == []
    assert c.get("/api/v1/harness", params={"scope": "nope"}).status_code == 404
    up = c.put("/api/v1/harness/scopes/beta", json={"kind": "company", "companies": ["C사"]}, headers=H).json()
    assert up["kind"] == "company" and up["companies"] == ["C사"] and up["roles"] == ["ops"]
    choices = {h["id"]: h for h in c.get("/api/v1/harnesses").json()}
    assert [r["name"] for r in choices["beta"]["roles"]] == ["ops"] and not choices["beta"]["auto"]
    bundle = c.get("/api/v1/settings").json()
    assert {h["id"] for h in bundle["harnesses"]} == {"acme", "beta", "proj-x"}
    assert c.delete("/api/v1/harness/scopes/beta", headers=H).status_code == 200
    assert not harness.root("beta").exists()
    assert c.delete("/api/v1/harness/scopes/beta", headers=H).status_code == 404
    # 실행: 고른 하네스의 용도로 시작 / 없는 하네스는 404
    assert c.post("/api/v1/runs", json={"task": "x", "harness": "nope"}, headers=H).status_code == 404


def test_harness_commands_for_slash_menu(scopes):
    """하네스 commands/*.md = 입력창 '/' 명령. 자료로는 보이지 않고, 폴더 대상 회사를 따른다."""
    import server.app as srv
    harness.write("commands/review.md", "---\ndescription: 바뀐 것 검토\nargument-hint: <파일>\n---\n$ARGUMENTS 를 검토해 줘.\n")
    harness.write("commands/report.md", "# 일일 보고\n오늘 한 일을 정리해 줘.\n", "acme")
    harness.write("commands/bad name.md", "x\n", "proj-x")                     # 빈칸이 든 이름은 '/' 로 부를 수 없다
    harness.write("commands/AGENT.md", "---\ncompanies: [A사]\n---\n", "proj-x")
    harness.write("commands/deploy.md", "배포 절차대로 해 줘.\n", "proj-x")
    got = {(c["scope"], c["name"]): c for c in harness.commands(None, ["", "acme", "proj-x"])}
    assert set(got) == {("", "review"), ("acme", "report"), ("proj-x", "deploy")}
    assert got[("", "review")]["hint"] == "<파일>" and got[("", "review")]["body"] == "$ARGUMENTS 를 검토해 줘."
    assert got[("acme", "report")]["description"] == "일일 보고"                 # 머리말이 없으면 제목
    assert {c["name"] for c in harness.commands("B사", ["", "proj-x"])} == {"review"}   # 대상 회사가 아닌 폴더는 뺀다
    assert not harness.search(None, "검토", scopes=[""])                          # 자료 검색에 섞이지 않는다
    c = TestClient(srv.app, base_url="http://127.0.0.1")
    listed = {(x["scope"], x["name"]) for x in c.get("/api/v1/commands").json()}
    assert listed == {("", "review"), ("acme", "report"), ("proj-x", "deploy")}
