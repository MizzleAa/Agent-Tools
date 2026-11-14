"""API 계약(OpenAPI)을 web/openapi.json 에 저장한다. 화면의 TypeScript 타입은 이 파일로 만든다.

    .venv\\Scripts\\python scripts\\export_openapi.py      # API 를 바꿨으면 실행
    cd web && npm run gen:api                              # → web/src/api/schema.d.ts

tests/test_api_contract.py 가 저장된 계약과 지금 코드가 같은지 확인한다 (다르면 이 스크립트를 실행하라고 알려 준다).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "web" / "openapi.json"


def schema_text() -> str:
    from server.app import app
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(schema_text(), encoding="utf-8")
    print(f"저장: {OUT}")
