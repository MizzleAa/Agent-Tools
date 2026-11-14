"""1단계-②: 도구(tool)를 알려주고 한 바퀴를 '손으로' 돌려 보기.

    .venv\\Scripts\\python learn\\02_tool_call.py

흐름
  1) 질문 + 도구 설명(JSON 스키마)을 보낸다
  2) 모델은 답 대신 "read_file 을 이 인자로 불러줘" 라는 JSON 을 돌려준다  ← 모델이 직접 실행하는 게 아니다
  3) 우리가(하네스가) 파일을 읽고, 결과를 role="tool" 메시지로 붙여 다시 보낸다
  4) 이제 모델이 실제 내용을 보고 답한다

이 1~4 를 자동으로 반복하는 것이 '에이전트 루프'다 → 03_tiny_loop.py
"""

import json
import sys
from pathlib import Path

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.config import load_settings  # noqa: E402

_s = load_settings()                     # 관리 화면에서 고른 모델·포트를 그대로 쓴다
BASE_URL = _s.backend.base_url          # llama-server (예: http://127.0.0.1:18080/v1)
MODEL = _s.backend.model

client = OpenAI(base_url=BASE_URL, api_key="local")

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "텍스트 파일 내용을 읽는다.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "파일의 절대 경로"}},
            "required": ["path"],
        },
    },
}]

messages = [{"role": "user", "content": r"C:\workspace\Agent-Tools\config\settings.yaml 파일에 적힌 model 값이 뭐야?"}]

# ── 1) 첫 호출: 모델이 도구 호출을 요청한다
r = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
msg = r.choices[0].message
print("=== 모델의 첫 응답 (원본 JSON) ===")
print(msg.model_dump_json(indent=2, exclude_none=True))

if not msg.tool_calls:
    print("\n모델이 도구를 부르지 않았습니다. 다시 실행해 보세요 (확률적으로 달라짐).")
    raise SystemExit

# ── 2) 하네스가 도구를 실행한다
messages.append(msg.model_dump(exclude_none=True, exclude={"reasoning_content"}))  # 사고 과정은 다시 보내지 않는다
for tc in msg.tool_calls:
    args = json.loads(tc.function.arguments)
    print(f"\n=== 하네스가 실행: {tc.function.name}({args}) ===")
    result = Path(args["path"]).read_text(encoding="utf-8")
    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

# ── 3) 결과를 붙여 다시 호출: 이제 실제 내용을 보고 답한다
r = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
print("\n=== 최종 답변 ===")
print(r.choices[0].message.content)
