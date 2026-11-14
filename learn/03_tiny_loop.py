"""1단계-③: 가장 작은 에이전트 루프 (안전장치 없음, 읽기 도구만).

    .venv\\Scripts\\python learn\\03_tiny_loop.py

에이전트의 본질은 아래 while 문 하나다. agent/loop.py 는 여기에 승인, 샌드박스, 한도,
출력 자르기, 이벤트를 더한 '실전판'이다.
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


def list_dir(path: str) -> str:
    return "\n".join(("[D] " if p.is_dir() else "[F] ") + p.name for p in sorted(Path(path).iterdir()))


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")[:4000]


FUNCS = {"list_dir": list_dir, "read_file": read_file}
TOOLS = [
    {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
    for name, desc in [("list_dir", "폴더 안의 항목 목록"), ("read_file", "텍스트 파일 읽기")]
]

messages = [
    {"role": "system", "content": "너는 파일을 도구로 직접 확인하고 답하는 비서다. 추측하지 않는다."},
    {"role": "user", "content": r"C:\workspace\Agent-Tools\config 폴더에 어떤 설정 파일이 있고 각각 무슨 역할인지 요약해줘."},
]

for step in range(1, 11):                       # 무한 루프 방지: 최대 10 스텝
    msg = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS).choices[0].message
    messages.append(msg.model_dump(exclude_none=True, exclude={"reasoning_content"}))
    if not msg.tool_calls:                       # 도구 호출이 없으면 = 최종 답변
        print(f"\n=== 최종 답변 (스텝 {step}) ===\n{msg.content}")
        break
    for tc in msg.tool_calls:                    # 도구 실행 → 결과를 기록에 붙인다
        args = json.loads(tc.function.arguments)
        print(f"[스텝 {step}] {tc.function.name}({args})")
        try:
            result = FUNCS[tc.function.name](**args)
        except Exception as e:                   # 오류도 모델에게 알려주면 스스로 고친다
            result = f"오류: {e}"
        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
