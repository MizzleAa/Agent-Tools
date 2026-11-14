"""API 계약 점검: 화면(web/)과 서버가 같은 모양을 쓰는지.

- web/openapi.json(화면 타입의 원본)이 지금 서버 코드와 같아야 한다 → 다르면 scripts/export_openapi.py + npm run gen:api
- 실행 이벤트의 API 모양(server/api/events.py)이 agent/events.py 와 필드가 같아야 한다
- 모든 API 는 응답 모양·요약이 있고, 오류는 {"detail": 문자열} 하나로 문서화된다
"""

import dataclasses
import json
from pathlib import Path

from agent import events as domain
from scripts.export_openapi import OUT, schema_text
from server.api.events import EVENT_MODELS
from server.app import app


def test_saved_contract_matches_code():
    assert OUT.exists(), "web/openapi.json 이 없습니다: .venv\\Scripts\\python scripts\\export_openapi.py"
    assert json.loads(OUT.read_text(encoding="utf-8")) == json.loads(schema_text()), \
        "API 가 바뀌었습니다: scripts/export_openapi.py 를 실행하고 web 에서 npm run gen:api 로 타입을 다시 만드세요"


def test_event_schemas_match_domain_events():
    classes = {c.__name__: c for c in vars(domain).values()
               if isinstance(c, type) and issubclass(c, domain.Event) and c is not domain.Event}
    assert set(classes) == set(EVENT_MODELS)
    for name, cls in classes.items():
        api_fields = set(EVENT_MODELS[name].model_fields) - {"type", "seq"}
        assert api_fields == {f.name for f in dataclasses.fields(cls)}, name
        sample = {f.name: _sample(f.type) for f in dataclasses.fields(cls)}
        EVENT_MODELS[name].model_validate({"type": name, **cls(**sample).to_dict()})   # 실제 이벤트가 그대로 읽힌다


def _sample(t):
    t = str(t)
    return 1 if t.startswith("int") else 1.0 if t.startswith("float") else True if t.startswith("bool") \
        else [] if t.startswith("list") else {} if t.startswith("dict") else "x"


def test_every_operation_is_typed_and_documented():
    schema = app.openapi()
    for path, ops in schema["paths"].items():
        assert path.startswith("/api/v1/"), path
        for method, op in ops.items():
            assert op.get("summary"), f"{method} {path} 요약 없음"
            assert "422" not in op["responses"], f"{method} {path}"
            ok = op["responses"].get("200") or op["responses"].get("201")
            assert ok, f"{method} {path} 성공 응답 없음"
            content = ok.get("content", {})
            if "text/event-stream" not in content:
                assert content["application/json"]["schema"], f"{method} {path} 응답 모양 없음"
            for code in ("400", "404"):
                ref = op["responses"][code]["content"]["application/json"]["schema"]["$ref"]
                assert ref.endswith("/ErrorResponse"), f"{method} {path} {code}"
