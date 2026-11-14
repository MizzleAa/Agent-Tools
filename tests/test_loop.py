from types import SimpleNamespace

from agent.events import ApprovalRequested, Final, LLMCall, RunStarted, ToolCall, ToolResult
from agent.llm import LLMClient, _parse_tool_call
from agent.config import Backend, Sampling
from agent.loop import Agent
from tests.conftest import FakeLLM, call, reply


def run(agent, task="작업"):
    return list(agent.run(task))


def of(events, kind):
    return [e for e in events if isinstance(e, kind)]


def test_answer_without_tools_ends_run(settings, profile):
    events = run(Agent(settings, profile, llm=FakeLLM([reply(content="안녕하세요")])))
    assert isinstance(events[0], RunStarted)
    final = events[-1]
    assert isinstance(final, Final) and final.status == "done" and final.answer == "안녕하세요"
    assert final.steps == 1


def test_tool_result_is_fed_back_to_model(settings, profile, ws):
    (ws / "a.txt").write_text("비밀 값은 42", encoding="utf-8")

    def second_turn(messages):
        assert messages[-1]["role"] == "tool" and "비밀 값은 42" in messages[-1]["content"]
        return reply(content="값은 42 입니다")

    events = run(Agent(settings, profile, llm=FakeLLM([reply(call("read_file", path="a.txt")), second_turn])))
    final = events[-1]
    assert final.status == "done" and "42" in final.answer and final.steps == 2
    assert final.tools_used == ["read_file"]


def test_unknown_tool_and_bad_arguments_return_errors_not_crash(settings, profile):
    llm = FakeLLM([reply(call("delete_everything"), call("read_file")), reply(content="끝")])
    results = of(run(Agent(settings, profile, llm=llm)), ToolResult)
    assert not results[0].ok and "도구는 없습니다" in results[0].output
    assert not results[1].ok and "인자가 잘못" in results[1].output


def test_write_requires_approval_and_denial_is_respected(settings, profile, ws):
    llm = FakeLLM([reply(call("write_file", path="out.md", content="x")), reply(content="거부되어 저장하지 못했습니다")])
    events = run(Agent(settings, profile, llm=llm, approver=lambda req: False))
    assert len(of(events, ApprovalRequested)) == 1
    assert "거부" in of(events, ToolResult)[0].output
    assert not (ws / "out.md").exists()


def test_write_approved_creates_file(settings, profile, ws):
    llm = FakeLLM([reply(call("write_file", path="sub/out.md", content="# 보고서")), reply(content="저장함")])
    run(Agent(settings, profile, llm=llm, approver=lambda req: True))
    assert (ws / "sub" / "out.md").read_text(encoding="utf-8") == "# 보고서"


def test_auto_approve_writes_skips_prompt_but_exec_still_asks(settings, profile, ws):
    profile.auto_approve_writes = True
    asked = []
    llm = FakeLLM([reply(call("write_file", path="a.txt", content="1"), call("run_python", code="print(1)")),
                   reply(content="끝")])
    run(Agent(settings, profile, llm=llm, approver=lambda req: asked.append(req.name) or False))
    assert asked == ["run_python"]
    assert (ws / "a.txt").exists()


def test_max_steps_forces_final_answer_without_tools(settings, profile):
    profile.max_steps = 3
    llm = FakeLLM([reply(call("list_dir", path=".")) for _ in range(3)] + [reply(content="여기까지 정리")])
    final = run(Agent(settings, profile, llm=llm))[-1]
    assert final.status == "max_steps" and final.answer == "여기까지 정리"
    assert llm.calls[-1]["tools"] is None


def test_cancel_before_start(settings, profile):
    agent = Agent(settings, profile, llm=FakeLLM([reply(content="x")]))
    agent.cancel.set()
    assert run(agent)[-1].status == "cancelled"


def test_long_tool_output_is_truncated_and_saved(settings, profile, ws):
    settings.limits.tool_output_chars = 500
    (ws / "big.txt").write_text("\n".join(f"line {i} " + "x" * 50 for i in range(300)), encoding="utf-8")
    llm = FakeLLM([reply(call("read_file", path="big.txt", max_lines=300)), reply(content="끝")])
    agent = Agent(settings, profile, llm=llm)
    result = of(run(agent), ToolResult)[0]
    assert "생략" in result.output and len(result.output) < 1000
    assert (agent.ctx.run_dir / f"{result.call_id}.txt").exists()


def test_reasoning_is_not_sent_back(settings, profile):
    llm = FakeLLM([reply(call("list_dir"), reasoning="비밀스러운 사고 과정"), reply(content="끝")])
    run(Agent(settings, profile, llm=llm))
    assert "비밀스러운 사고 과정" not in str(llm.calls[1]["messages"])


def test_event_order(settings, profile):
    llm = FakeLLM([reply(call("list_dir")), reply(content="끝")])
    kinds = [type(e).__name__ for e in run(Agent(settings, profile, llm=llm))]
    assert kinds == ["RunStarted", "LLMCall", "ToolCall", "ToolResult", "LLMCall", "Final"]
    assert all(isinstance(e.to_dict(), dict) for e in run(Agent(settings, profile, llm=FakeLLM([reply(content="x")]))))


def _fn(arguments):
    return SimpleNamespace(id="c1", function=SimpleNamespace(name="read_file", arguments=arguments))


def test_malformed_json_arguments_are_repaired():
    assert _parse_tool_call(_fn('{"path": "a.txt",}')).arguments == {"path": "a.txt"}
    assert _parse_tool_call(_fn("")).arguments == {}
    assert _parse_tool_call(_fn('"just a string"')).error


def test_think_tags_are_split_from_content():
    client = LLMClient(Backend(), Sampling())
    message = SimpleNamespace(content="<think>생각 중</think>\n답변", tool_calls=None, model_extra={})
    completion = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                                 usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: completion)))
    r = client.chat([{"role": "user", "content": "hi"}])
    assert r.content == "답변" and r.reasoning == "생각 중"
