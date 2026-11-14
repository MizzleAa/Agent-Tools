"""시험용 가짜 CLI — `app-server` 면 Codex app-server, `--acp` 면 Gemini ACP 처럼 줄 단위 JSON 으로 답한다.
(tests/test_cli_engines.py — 진짜 CLI · 로그인 없이 드라이버 · 승인 · 기록 · 재개를 시험한다.)
받은 지침 · 요청은 FAKE_ENGINE_LOG 파일에 한 줄씩 적는다.
"""
from __future__ import annotations

import json
import os
import sys

LOG = os.environ.get("FAKE_ENGINE_LOG")
GEMINI = "--acp" in sys.argv
OUT_ID = [0]
pending: dict[int, dict] = {}


def log(kind: str, data) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": kind, "data": data}, ensure_ascii=False) + "\n")


def send(obj: dict) -> None:
    if GEMINI:
        obj = {"jsonrpc": "2.0", **obj}
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def note(method: str, params: dict) -> None:
    send({"method": method, "params": params})


def ask(method: str, params: dict) -> dict:
    """서버→클라이언트 요청을 보내고 답을 기다린다 (그 사이 온 다른 메시지는 처리)."""
    rid = OUT_ID[0]
    OUT_ID[0] += 1
    send({"id": rid, "method": method, "params": params})
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        msg = json.loads(line)
        if msg.get("id") == rid and "method" not in msg:
            return msg.get("result") or {}
        handle(msg)


# ───────────────────────── Codex app-server

def codex_turn(params: dict) -> None:
    thread = params["threadId"]
    note("turn/started", {"threadId": thread, "turn": {"id": "turn-1", "status": "inProgress"}})
    note("item/started", {"item": {"type": "reasoning", "id": "r1", "summary": []}})
    note("item/reasoning/summaryTextDelta", {"itemId": "r1", "delta": "생각 중"})
    note("item/completed", {"item": {"type": "reasoning", "id": "r1", "summary": ["생각 중"]}})
    cmd = '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command \'echo hello\''
    note("item/started", {"item": {"type": "commandExecution", "id": "c1", "command": cmd, "cwd": "C:\\w", "status": "inProgress"}})
    r = ask("item/commandExecution/requestApproval", {"threadId": thread, "turnId": "turn-1", "itemId": "c1", "command": cmd, "cwd": "C:\\w"})
    log("decision", r.get("decision"))
    ok = r.get("decision") == "accept"
    note("item/completed", {"item": {"type": "commandExecution", "id": "c1", "status": "completed" if ok else "declined",
                                     "exitCode": 0 if ok else None, "aggregatedOutput": "hello\r\n" if ok else ""}})
    note("item/started", {"item": {"type": "agentMessage", "id": "a1", "text": ""}})
    for d in ("다 ", "했습니다"):
        note("item/agentMessage/delta", {"itemId": "a1", "delta": d})
    note("item/completed", {"item": {"type": "agentMessage", "id": "a1", "text": "다 했습니다"}})
    note("thread/tokenUsage/updated", {"tokenUsage": {"total": {"inputTokens": 120, "outputTokens": 30, "cachedInputTokens": 5}}})
    note("turn/completed", {"threadId": thread, "turn": {"id": "turn-1", "status": "completed", "durationMs": 42}})


def handle_codex(msg: dict) -> None:
    m, p, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if m == "initialize":
        send({"id": rid, "result": {"userAgent": "fake", "codexHome": "x"}})
    elif m == "account/read":
        send({"id": rid, "result": {"account": {"type": "chatgpt", "email": "t@example.com", "planType": "plus"}}})
    elif m == "model/list":
        send({"id": rid, "result": {"data": [{"id": "gpt-fake", "isDefault": True, "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]}})
    elif m in ("thread/start", "thread/resume"):
        log(m, p)
        tid = p.get("threadId") or "thr-fake-1"
        send({"id": rid, "result": {"thread": {"id": tid, "turns": []}, "model": p.get("model") or "gpt-fake"}})
    elif m == "turn/start":
        log("turn/start", p)
        send({"id": rid, "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
        codex_turn(p)
    elif m == "turn/interrupt":
        send({"id": rid, "result": {}})
    elif rid is not None:
        send({"id": rid, "error": {"code": -32600, "message": f"unknown {m}"}})


# ───────────────────────── Gemini ACP

def gemini_prompt(rid, p: dict) -> None:
    sid = p["sessionId"]
    log("session/prompt", p)

    def upd(u: dict) -> None:
        note("session/update", {"sessionId": sid, "update": u})

    upd({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "**계획**"}})
    upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "파일을 "}})
    upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "만듭니다"}})
    call = {"toolCallId": "w1", "status": "pending", "title": "Writing to a.txt", "kind": "edit",
            "locations": [{"path": "a.txt"}], "content": [{"type": "diff", "path": "a.txt", "oldText": None, "newText": "hi"}]}
    upd({"sessionUpdate": "tool_call", **call})
    r = ask("session/request_permission", {"sessionId": sid, "toolCall": call, "options": [
        {"optionId": "proceed_always", "name": "Allow for this session", "kind": "allow_always"},
        {"optionId": "proceed_once", "name": "Allow", "kind": "allow_once"},
        {"optionId": "cancel", "name": "Reject", "kind": "reject_once"}]})
    log("permission", r)
    ok = (r.get("outcome") or {}).get("optionId") == "proceed_once"
    upd({"sessionUpdate": "tool_call_update", "toolCallId": "w1", "status": "completed" if ok else "failed",
         "content": [{"type": "content", "content": {"type": "text", "text": "썼습니다" if ok else "거부됨"}}]})
    upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "[MODE_UPDATE] default"}})
    upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "끝"}})
    send({"id": rid, "result": {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}})


def handle_gemini(msg: dict) -> None:
    m, p, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if m == "initialize":
        send({"id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}, "authMethods": []}})
    elif m == "session/new":
        log("session/new", p)
        send({"id": rid, "result": {"sessionId": "gs-fake-1", "models": {"availableModels": [{"modelId": "gemini-fake", "name": "Fake"}],
                                                                           "currentModelId": "gemini-fake"}}})
        note("session/update", {"sessionId": "gs-fake-1", "update": {"sessionUpdate": "available_commands_update", "availableCommands": [
            {"name": "memory", "description": "Manage memory.", "input": None},
            {"name": "restore", "description": "Restore a checkpoint.", "input": {"hint": "<checkpoint>"}}]}})
    elif m == "session/load":
        log("session/load", p)
        note("session/update", {"sessionId": p["sessionId"], "update": {"sessionUpdate": "user_message_chunk",
                                                                         "content": {"type": "text", "text": "옛 기록"}}})
        note("session/update", {"sessionId": p["sessionId"], "update": {"sessionUpdate": "agent_message_chunk",
                                                                         "content": {"type": "text", "text": "옛 답"}}})
        send({"id": rid, "result": {"models": {"availableModels": [], "currentModelId": "gemini-fake"}}})
    elif m in ("session/set_model", "session/set_mode"):
        log(m, p)
        send({"id": rid, "result": {}})
    elif m == "session/prompt":
        gemini_prompt(rid, p)
    elif m == "session/cancel":
        log("cancel", p)
    elif rid is not None:
        send({"id": rid, "error": {"code": -32601, "message": f"Method not found: {m}"}})


def handle(msg: dict) -> None:
    (handle_gemini if GEMINI else handle_codex)(msg)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if line:
            handle(json.loads(line))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    log("argv", sys.argv[1:])
    main()
