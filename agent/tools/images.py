"""여러 이미지를 한 번에 채팅 화면에 붙이는 도구 (기능 요구 '이미지 표시' — 묶어 붙이기).

기본 용도(intranet)는 api_get 으로 이미지를 돌려주는 경로를 부르면 화면에 붙는다 (도구 수를 늘리지 않는다).
이 도구는 한 번에 여러 장을 붙이고 싶을 때 프로필에 넣어 쓴다. 저장·기록은 agent/image_store.py 가 한다.
- 이미지는 관리 서버가 등록된 API 에서 직접 받는다 (인증은 서버 쪽 요청 헤더에만 — 주소·모델 입력·기록에 없다).
- 모델에게는 장마다 '표시됨 / 실패(이유)' 한 줄만 돌아간다 (16k 컨텍스트 보호, 모델은 텍스트 전용).
- 경로는 문서에 있는 GET 경로만, 식별자는 이번 실행에서 받은 결과(또는 사용자 말)에 있는 값만 (지어낸 이름은 조회하지 않고 실패로 그린다).
"""

from __future__ import annotations

import json
from typing import Annotated, Any
from urllib.parse import unquote

from pydantic import Field

from agent import grounding, image_store, imaging
from agent.image_store import THUMB_SIDE, VIEW_SIDE, image_dir, label_of  # noqa: F401 — 화면 서버·테스트가 여기서 가져간다
from agent.tools.api import build_url, check_documented, empty_result, exchange, find_api
from agent.tools.base import ToolContext, ToolError, tool
from agent.tools.web import check_url

MAX_PER_CALL = 12            # 한 번에 붙이는 최대 장수 (화면·서버 부담)
IMAGE_WORDS = "image images thumbnail thumb preview original photo picture media file download raw bytes jpeg png bitmap"


def _image_paths_hint(api) -> str:
    from agent import openapi
    found = [line for line in openapi.search_docs(api.docs, IMAGE_WORDS, limit=10) if line.startswith("GET ")][:5]
    return ("\n  ".join(f"{line[:140]}" for line in found)) if found else ""


def _download(ctx: ToolContext, api, path: str) -> tuple[bytes, str, str]:
    """→ (바이트, 형식, 실패 이유). 실패면 바이트가 비고 이유가 있다."""
    seen = grounding.seen_of(ctx.memo)
    try:
        check_documented(api, path)
        url = build_url(api, path)
        check_url(ctx.settings, url)
        if seen is not None and (made_up := grounding.unseen(seen, grounding.path_identifiers(path))):
            return b"", "", f"이번 실행의 결과에 없는 이름이라 조회하지 않음 ({made_up[0][:60]}) — 지어낸 이름일 수 있음"
        r, _, _ = exchange(ctx.settings, api, "GET", url, accept="image/*,*/*;q=0.5", timeout=max(60, ctx.settings.web.fetch_timeout))
    except ToolError as e:
        return b"", "", str(e).splitlines()[0][:160]
    if r.status_code >= 400:
        return b"", "", {401: "인증이 거절됨 (HTTP 401)", 403: "권한 없음 (HTTP 403)",
                         404: "찾을 수 없음 (HTTP 404)"}.get(r.status_code, f"받지 못함 (HTTP {r.status_code})")
    data = r.content
    fmt = imaging.sniff(data)
    if fmt:
        return data, fmt, ""
    ctype = r.headers.get("content-type", "-").split(";")[0]
    if "json" in ctype:
        try:
            body = json.loads(data)
        except ValueError:
            body = None
        if body is not None and empty_result(body, path):
            return b"", "", "찾을 수 없음 (대상이 없다는 빈 응답)"
    return b"", "", f"이미지가 아닌 응답 ({ctype})"


@tool("read")
def show_images(
    ctx: ToolContext,
    api: Annotated[str, Field(description="등록된 API 이름")],
    paths: Annotated[list[str], Field(description="이미지를 돌려주는 GET 경로들 (문서에서 image·thumbnail·preview·original 등). "
                                                  "식별자는 받은 결과에 있는 값만. 최대 12개")],
    labels: Annotated[list[str], Field(description="각 이미지 아래 보일 이름 (파일명 등, paths 와 같은 순서). 비우면 경로에서 뽑는다")] = [],  # noqa: B006
) -> str:
    """사내 API 의 이미지 여러 장을 받아 채팅 화면(답변 아래)에 썸네일로 붙인다. 사용자가 이미지를 보여 달라고 할 때만 쓴다.
    모델은 이미지 내용을 볼 수 없다 — 장마다 표시됐는지·실패했는지만 돌아온다."""
    a = find_api(ctx.settings, api)
    wanted = list(dict.fromkeys(p.strip() for p in paths if isinstance(p, str) and p.strip()))
    if not wanted:
        raise ToolError("paths 에 이미지를 돌려주는 GET 경로를 하나 이상 주세요")
    left = min(MAX_PER_CALL, image_store.room(ctx.memo))
    if left <= 0:
        raise ToolError(f"이 실행에서 화면에 붙일 수 있는 이미지({image_store.MAX_PER_RUN}장)를 다 썼습니다")
    take, skipped = wanted[:left], wanted[left:]
    got = [_download(ctx, a, take[0])]   # 첫 장으로 로그인을 끝낸 뒤 나머지는 나눠 받는다
    if take[1:]:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            got += list(pool.map(lambda p: _download(ctx, a, p), take[1:]))
    seen = grounding.seen_of(ctx.memo)
    items: list[dict[str, Any]] = []
    for i, (path, (data, fmt, reason)) in enumerate(zip(take, got)):
        given = labels[i].strip() if i < len(labels) and isinstance(labels[i], str) else ""
        ok_label = given and (given in unquote(path) or (seen is not None and seen.has(given)))   # 이미지와 이름이 엇갈리지 않게
        items.append(image_store.record(ctx.memo, ctx.run_dir, a.name, path, ok=bool(data), reason=reason, data=data,
                                        fmt=fmt, label=given[:120] if ok_label else ""))
    ok = sum(it["ok"] for it in items)
    lines = [f"{k}) {it['label']} — " + (f"표시됨 ({it['format']} {it['width']}×{it['height']})" if it["ok"]
                                          else f"실패: {it['reason']}") for k, it in enumerate(items, 1)]
    out = (f"채팅 화면에 이미지 {len(items)}장을 붙였습니다 (표시 {ok} · 실패 {len(items) - ok}). 이미지 내용은 볼 수 없으니 "
           "답에는 무엇을 붙였는지(이름)와 실패한 장의 이유만 쓰세요:\n" + "\n".join(lines))
    if skipped:
        out += f"\n(한 번에 {MAX_PER_CALL}장까지라 {len(skipped)}장은 붙이지 않음 — 필요하면 나눠서 다시 부르세요)"
    if any("이미지가 아닌 응답" in it["reason"] for it in items) and (hint := _image_paths_hint(a)):
        out += f"\n이미지 대신 데이터(JSON)를 돌려주는 경로였습니다. 문서에서 이미지를 돌려주는 경로 후보:\n  {hint}"
    return out
