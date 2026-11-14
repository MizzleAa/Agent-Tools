"""1단계-①: 모델을 '그냥' 호출해 보기.

    .venv\\Scripts\\python learn\\01_raw_call.py
    (먼저 모델 서버가 켜져 있어야 한다: start.cmd 또는 scripts\\check_env.py --load 참고)

관찰할 것
- 모델은 내 PC 의 파일을 볼 수 없다 → 그럴듯하게 지어내거나, 모른다고 한다.
- Qwen3 는 답하기 전에 '사고 과정(reasoning)'을 따로 내보낸다.
- 이것이 "명령어 한 줄로는 안 되는" 이유다. 모델은 텍스트를 한 번 출력할 뿐이다.
"""

import sys
from pathlib import Path

from openai import OpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.config import load_settings  # noqa: E402

_s = load_settings()                     # 관리 화면에서 고른 모델·포트를 그대로 쓴다
BASE_URL = _s.backend.base_url          # llama-server (예: http://127.0.0.1:18080/v1)
MODEL = _s.backend.model

client = OpenAI(base_url=BASE_URL, api_key="local")
question = r"C:\workspace\Agent-Tools\config\settings.yaml 파일에 적힌 model 값이 뭐야?"

r = client.chat.completions.create(model=MODEL, messages=[{"role": "user", "content": question}])
msg = r.choices[0].message

print("=== 사고 과정 (reasoning) ===")
print(((msg.model_extra or {}).get("reasoning_content") or "(없음)")[:1500])
print("\n=== 답변 (content) ===")
print(msg.content)
print("\n=== 토큰 사용량 ===")
print(r.usage)
