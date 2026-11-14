"""사내 API 도구. settings.yaml 의 apis 에 등록된 API 만 호출할 수 있다.

- api_get  : 조회(GET). 자동 실행.
- api_send : 변경(POST/PUT/PATCH/DELETE). 매번 사람의 승인 (permission="send").
- 인증 토큰은 환경변수 / config/secrets.yaml 에서 읽어 헤더에만 넣는다. 모델은 토큰을 볼 수 없다.
- 응답은 사내 데이터지만 자유 입력 필드에 숨은 지시가 있을 수 있어 taints=True 로 둔다.
"""

from __future__ import annotations

import json
import re
import time
from typing import Annotated, Any, Literal
from urllib.parse import unquote, unquote_plus, urlsplit

import httpx
import json_repair
from pydantic import Field

from agent import apiauth, grounding, image_store, imaging
from agent.config import ApiEndpoint, Settings
from agent.tools.base import Failed, ToolContext, ToolError, decode_bytes, tool
from agent.tools.web import USER_AGENT, check_url


def find_api(settings: Settings, name: str) -> ApiEndpoint:
    for api in settings.apis:
        if api.name == name:
            return api
    names = ", ".join(a.name for a in settings.apis) or "(등록된 API 없음)"
    raise ToolError(f"등록되지 않은 API 입니다: {name}. 사용 가능: {names}")


def build_url(api: ApiEndpoint, path: str) -> str:
    """base_url 뒤에 경로를 붙인다. 다른 호스트나 base_url 바깥 경로로 나가는 것은 막는다."""
    p = path.strip()
    if not p.startswith("/") or p.startswith("//") or "://" in p or "\\" in p or "@" in p:
        raise ToolError("path 는 '/' 로 시작하는 API 경로여야 합니다. 예) /lots/123?line=A1")
    base = api.base_url.rstrip("/")
    url = base + p
    b, u = urlsplit(base), urlsplit(url)
    if ".." in unquote(u.path).split("/"):
        raise ToolError("경로에 '..' 을 쓸 수 없습니다")
    if (u.scheme, u.netloc) != (b.scheme, b.netloc) or not u.path.startswith(b.path):
        raise ToolError(f"'{api.name}' API 의 주소({base}) 밖으로 나가는 경로입니다")
    return url


MAX_UPLOAD_BYTES = 500 * 1024 * 1024


def exchange(settings: Settings, api: ApiEndpoint, method: str, url: str, request: dict[str, Any] | None = None,
             timeout: float = 0, accept: str = "application/json") -> tuple[httpx.Response, bool, dict[str, str]]:
    """인증을 붙여 보낸다 → (응답, 계정 없이 보냈는지, 붙인 비밀 헤더). 토큰은 헤더에만 넣는다 (주소·모델 입력에는 없다).
    - API-18: 계정이 아직 없으면 인증 없이 먼저 보내 본다 (인증이 필요 없는 경로도 있다). 401·403 이면 계정을 안내한다.
    - 로그인 토큰이 만료돼 401 이면 다시 로그인해 한 번 더 보낸다."""
    check = lambda u: check_url(settings, u)   # noqa: E731  로그인 주소도 같은 규칙으로
    anonymous = api.auth is not None and bool(apiauth.missing(api))   # 로그인 사용 모드면 그 사람의 계정 기준
    secret_headers = {} if anonymous else apiauth.headers(api, check)
    headers = {"Accept": accept, "User-Agent": USER_AGENT, **api.headers, **secret_headers}

    def send() -> httpx.Response:
        try:
            with httpx.Client(timeout=timeout or settings.web.fetch_timeout, follow_redirects=False,
                              verify=api.verify_tls) as c:
                return c.request(method, url, headers=headers, **(request or {}))
        except httpx.HTTPError as e:
            raise ToolError(f"API 호출 실패 ({type(e).__name__}: {e})")

    r = send()
    if anonymous and r.status_code in (401, 403):
        raise ToolError(apiauth.missing_message(api))
    if r.status_code == 401 and not anonymous and api.auth is not None and api.auth.kind == "login":   # 토큰 만료 → 다시 로그인
        apiauth.forget(api)
        secret_headers = apiauth.headers(api, check, fresh=True)
        headers.update(secret_headers)
        r = send()
    return r, anonymous, secret_headers


def _call(ctx: ToolContext, api_name: str, method: str, path: str, body: str = "",
          file_path: str = "", file_field: str = "file", count_by: str = "") -> str:
    api = find_api(ctx.settings, api_name)
    if method not in api.allow_methods:
        hint = f" 사용자가 관리 화면 '상태 · 연결' › 사내 API › '{api.name}' 에서 '변경 요청 허용'을 켜야 합니다." if method != "GET" else ""
        raise ToolError(f"'{api.name}' API 는 {method} 가 허용되지 않습니다 (허용: {', '.join(api.allow_methods)}).{hint}")
    url = build_url(api, path)
    check_url(ctx.settings, url)
    seen = grounding.seen_of(ctx.memo)
    if seen is not None and (made_up := grounding.unseen(seen, grounding.path_identifiers(path))):
        if method == "GET" and image_store.is_image_endpoint(api.docs, path):   # 이미지 자리에 '찾을 수 없음'을 그린다
            image_store.record(ctx.memo, ctx.run_dir, api.name, path, ok=False,
                               reason="이번 실행의 결과에 없는 이름이라 조회하지 않음 — 지어낸 이름일 수 있음")
        raise ToolError(_made_up_message(ctx, made_up))   # S-8: 결과에 없던 식별자로 조회하지 않는다
    payload = None
    if body.strip():
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = json_repair.loads(body)
    request: dict[str, Any] = {"json": payload}
    if file_path:   # 파일 업로드(multipart/form-data). body 의 키·값은 함께 보낼 폼 필드가 된다
        local = ctx.sandbox.resolve(file_path)   # 작업 폴더 밖 파일은 보낼 수 없다
        if not local.is_file():
            raise ToolError(f"파일이 없습니다: {file_path}")
        if local.stat().st_size > MAX_UPLOAD_BYTES:
            raise ToolError(f"파일이 너무 큽니다 (최대 {MAX_UPLOAD_BYTES // 2**20}MB): {file_path}")
        form = {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in (payload or {}).items()} \
            if isinstance(payload, dict) else {}
        request = {"data": form, "files": {file_field: (local.name, local.read_bytes())}}
    r, anonymous, secret_headers = exchange(ctx.settings, api, method, url, request,
                                            timeout=max(ctx.settings.web.fetch_timeout, 120 if file_path else 0))
    if r.status_code < 300 and (binary := _binary_reply(ctx, api, method, path, r)):
        return binary   # 이미지는 화면에 붙이고, 이진 응답은 글로 풀지 않는다 (모델 입력에 바이트가 들어가지 않게)

    raw = r.content[: ctx.settings.web.max_fetch_bytes]
    text = decode_bytes(raw)
    counts = total = ""
    failure = failure_note(api, path, r.status_code) if r.status_code >= 400 else ""   # S-9: 거절 응답은 데이터가 아니다
    if "json" in r.headers.get("content-type", ""):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            if r.status_code < 300 and (nulls := empty_result(data, path)):
                failure = (f"⚠ 빈 결과: 요청한 대상이 없다는 뜻입니다 ({', '.join(nulls)} 가 null 이고 나머지도 비어 있음 — 없는 대상에도 "
                           "200 을 돌려주는 API 가 있다). 이 응답은 데이터가 아닙니다 — '0개'로 세거나 보고에 쓰지 말고, 요청한 값이 "
                           "앞서 받은 결과에 실제로 있는 값인지 확인하세요")
            elif r.status_code < 300 and seen is not None:
                seen.add(text)
                seen.add(json.dumps(data, ensure_ascii=False))
            if not failure:
                counts = count_block(data, count_by) if count_by else auto_counts(data)
                if count_by and (empty := _null_counts(counts)):   # S-15: 모두 비어 있는 필드로 세려 함
                    counts = f"{counts}\n{_null_count_note(api, path, empty)}"
                image_store.remember_names(ctx.memo, data)   # IMG-3: ID 로 부른 이미지도 이름으로 보이게
                if method == "GET":
                    total = running_totals(ctx, api, path, data)
                if method == "GET" and (ignored := ignored_params(path, data) + ignored_filters(path, data)):
                    counts = f"{ignored}\n{counts}".strip()   # S-10: 문서에 없는 파라미터가 무시됐다
                if method == "GET" and (empty := _empty_list_note(ctx, api, path, data)):
                    counts = f"{counts}\n{empty}".strip()     # S-12: 빈 목록 하나로 '없다'고 단정하지 않게
            text = json.dumps(shorten_opaque(data), ensure_ascii=False, indent=1)
            if not failure and len(text) > PAGE_CHARS and (index := item_index(data)):
                counts = f"{counts}\n{index}".strip()   # S-8: 잘려 안 보이는 항목도 이름은 보이게
    elif r.status_code < 300 and seen is not None:
        seen.add(text)
    # 서버가 토큰·계정을 되돌려 보내도 모델에게는 가린다 ("Bearer xxx" 전체, 접두어 뺀 "xxx", 아이디·비밀번호)
    for value in sorted(apiauth.secret_values(api, secret_headers), key=len, reverse=True):
        text = text.replace(value, "***")
        counts = counts.replace(value, "***")   # 집계 줄도 응답에서 나온 값이다 — 같이 가린다
        total = total.replace(value, "***")
    note = ""
    if r.is_redirect:
        note = f"\n(리다이렉트 응답: {r.headers.get('location', '')}. 리다이렉트는 따라가지 않습니다)"
    if r.status_code == 401:   # API-1: 사용자가 실제로 할 수 있는 다음 단계를 알려 준다
        note += "\n" + (f"(인증이 필요한데 이 API 에는 인증 설정이 없습니다. 사용자가 {apiauth.ui_place(api)} 에서 인증 방식"
                        "(토큰 또는 계정 로그인)을 정해야 합니다. 비밀번호·토큰은 채팅으로 받지 않습니다)" if api.auth is None else
                        f"(저장된 인증으로도 거절됐습니다. 사용자가 {apiauth.ui_place(api)} 에서 값을 확인해야 합니다)")
    elif anonymous:
        note += "\n(계정 없이 조회됨 — 이 경로는 인증이 필요 없다. 다른 경로는 계정이 필요할 수 있음)"
    if count_by and not counts and not failure:
        counts = f"(count_by='{count_by}': JSON 응답이 아니어서 세지 못함)"
    head = f"{method} {api.name}{path} → HTTP {r.status_code} ({r.headers.get('content-type', '-')}){note}"
    if failure:
        head += "\n" + failure
    if failure and method == "GET" and image_store.is_image_endpoint(api.docs, path):   # 이미지 자리에 실패를 그린다
        why = "찾을 수 없음 (대상이 없다는 빈 응답)" if failure.startswith("⚠ 빈 결과") else \
            {401: "인증이 거절됨", 403: "권한 없음", 404: "찾을 수 없음"}.get(r.status_code, "받지 못함") + f" (HTTP {r.status_code})"
        head += "\n" + image_store.line_for(image_store.record(ctx.memo, ctx.run_dir, api.name, path, ok=False, reason=why))
    if total and len(head) + len(counts) + len(text) + len(total) + 8 > PAGE_CHARS:
        counts = f"{counts}\n{total}".strip()   # 길어서 잘릴 응답이면 누계를 앞에 (잘린 뒤쪽은 안 보인다)
        total = ""
    out = head + (f"\n\n{counts}" if counts else "") + f"\n\n{text}"
    if total:   # 누계는 이 응답 하나의 값과 헷갈리지 않게 응답 뒤에 둔다
        out += f"\n\n{total}"
    return Failed(out) if failure else out


_TEXTUAL = re.compile(r"json|text|xml|html|javascript|yaml|csv|x-www-form", re.I)


def _binary_reply(ctx: ToolContext, api: ApiEndpoint, method: str, path: str, r: httpx.Response) -> str:
    """이미지·이진 응답이면 모델에게 줄 짧은 글 (아니면 빈 글자). 이미지는 화면에 붙인다 (기능 요구 '이미지 표시')."""
    data = r.content
    ctype = r.headers.get("content-type", "")
    head = f"{method} {api.name}{path} → HTTP {r.status_code} ({ctype or '-'})"
    fmt = imaging.sniff(data[:64])
    if fmt and method == "GET":
        if image_store.room(ctx.memo) <= 0:
            return (f"{head}\n\n이미지 응답 ({fmt}, {len(data) // 1024}KB) — 이 실행에서 화면에 붙일 수 있는 수"
                    f"({image_store.MAX_PER_RUN}장)를 넘어 붙이지 않았습니다. 이미지 내용은 볼 수 없습니다.")
        entry = image_store.record(ctx.memo, ctx.run_dir, api.name, path, ok=True, data=data, fmt=fmt)
        out = f"{head}\n\n{image_store.line_for(entry)}"
        return out if entry["ok"] else Failed(out)
    if fmt or (not _TEXTUAL.search(ctype) and b"\x00" in data[:4096]):
        return f"{head}\n\n이진 응답 ({len(data)}바이트) — 글이 아니라서 내용은 넣지 않았습니다 (모델은 이 내용을 볼 수 없음)."
    return ""


def failure_note(api: ApiEndpoint, path: str, status: int) -> str:
    """거절·오류 응답에 붙일 말: 데이터가 아니라는 것과 실제로 할 수 있는 다음 단계 (QA S-9)."""
    end = " 끝내 못 하면 이 단계는 보고에 '실패 (이유)'로 적으세요."
    if status == 401:
        return f"⚠ 실패: 인증이 거절됐습니다 (HTTP 401). 이 응답은 데이터가 아닙니다.{end}"
    if status == 403:
        others = "\n".join(f"  {line[:140]}" for line in _same_group(api, path))
        alt = (" 같은 정보를 이 계정 권한으로 주는 다른 조회 경로가 문서에 있는지 보세요 (같은 그룹의 조회 경로:\n"
               f"{others})" if others else "")
        return f"⚠ 실패: 권한이 없어 거절됐습니다 (HTTP 403). 이 응답은 데이터가 아닙니다 — 여기서 값을 만들지 마세요.{alt}{end}"
    if status == 404:
        return ("⚠ 실패: 대상이나 경로가 없습니다 (HTTP 404). 이 응답은 데이터가 아닙니다 — 경로의 값(식별자)이 앞서 받은 결과에 "
                f"실제로 있는 값인지 확인하세요.{_name_in_id_slot(api, path)}{end}")
    if status >= 500:
        return f"⚠ 실패: 서버 오류입니다 (HTTP {status}). 이 응답은 데이터가 아닙니다 — 한 번 더 해 보고, 계속되면 그만두세요.{end}"
    return f"⚠ 실패: 요청이 거절됐습니다 (HTTP {status}). 이 응답은 데이터가 아닙니다 — 응답의 이유를 보고 요청을 고치세요.{end}"


def _name_in_id_slot(api: ApiEndpoint, path: str) -> str:
    """ID 자리({lot_id} 등)에 이름처럼 생긴 값을 넣어 404 면, 이름이 아니라 ID 가 필요하다고 알려 준다 (QA S-12 재현: 이름을 ID 자리에
    넣어 404 → '로트가 존재하지 않음' 으로 단정). 그 자리 앞까지의 목록 경로가 문서에 있으면 함께."""
    from agent import openapi
    base = path.split("?")[0].rstrip("/")
    given = base.strip("/").split("/")
    for line in api.docs.splitlines():
        m = openapi.ENDPOINT_LINE.match(line)
        if not (m and m.group(1) == "GET" and _template(m.group(2).split("?")[0]).match(base)):
            continue
        parts = m.group(2).split("?")[0].strip("/").split("/")
        for i, slot in enumerate(parts):
            if i >= len(given) or not re.fullmatch(r"\{[^}]*id\}", slot, re.I):
                continue
            value = unquote(given[i])
            if value.isdigit() or grounding.is_identifier(value) or sum(c.isdigit() for c in value) >= 3:
                continue   # ID 처럼 생겼다
            listing = "/" + "/".join(parts[:i])
            has_list = any((n := openapi.ENDPOINT_LINE.match(x)) and n.group(1) == "GET" and n.group(2).split("?")[0].rstrip("/") == listing
                           for x in api.docs.splitlines())
            where = f" GET {listing} 목록에서 그 이름의 ID 를 찾아 다시 부르세요." if has_list else " 목록 경로에서 그 이름의 ID 를 찾아 다시 부르세요."
            return (f" '{value}' 는 ID 자리({slot})에 넣은 이름입니다 — 이 404 는 '없다'는 뜻이 아닐 수 있습니다.{where} "
                    "이 404 하나로 '없다'고 단정하지 마세요.")
    return ""


def _same_group(api: ApiEndpoint, path: str, k: int = 6) -> list[str]:
    """문서에서 같은 첫 경로 조각을 쓰는 다른 조회 경로 (권한이 다른 '내 것' 경로 등을 찾게)."""
    from agent import openapi
    base = path.split("?")[0]
    first = base.strip("/").split("/")[0]
    out = []
    for line in api.docs.splitlines():
        m = openapi.ENDPOINT_LINE.match(line)
        if not m or m.group(1) != "GET":
            continue
        p = m.group(2).split("?")[0]
        if p.strip("/").split("/")[0] == first and not _template(p).match(base):
            out.append(line)
    return out[:k]


_ID_KEY = re.compile(r"(?:^|_)(?:id|uuid|guid)$|[a-z]Id$")


def empty_result(data: Any, path: str) -> list[str]:
    """200 인데 대상이 없다는 응답인지: 식별 필드(…_id)가 null 이고 나머지가 모두 비었거나(0·빈 글자·빈 목록) 요청 값을
    되돌려 준 것뿐. 맞으면 null 인 식별 필드 이름들. (모두 0 인 집계처럼 식별 필드가 없는 응답은 정상 결과다)"""
    if not isinstance(data, dict) or len(data) < 2:
        return []
    nulls = [k for k, v in data.items() if v is None and _ID_KEY.search(k)]
    if not nulls:
        return []
    asked = unquote(path).lower()
    for v in data.values():
        if v is None or v in ("", [], {}) or (isinstance(v, (int, float)) and v == 0):
            continue
        if isinstance(v, str) and v.lower() in asked:
            continue
        return []
    return nulls


def main_list(data: Any) -> tuple[str, list[Any]] | None:
    """응답의 주 목록: 응답 자체가 목록이거나, 맨 위 값 중 가장 긴 목록."""
    if isinstance(data, list):
        return "", data
    if isinstance(data, dict):
        lists = [(k, v) for k, v in data.items() if isinstance(v, list)]
        if lists:
            return max(lists, key=lambda kv: len(kv[1]))
    return None


_COUNT_PARAM = re.compile(r"^(limit|size|count|per_?page|page_?size|pagesize|perpage|top|max|max_?results|take|first|"
                          r"n|num|rows|length)$", re.I)
_PAGE_FIELD = re.compile(r"^(page|size|per_?page|page_?size|limit|offset|skip|take|count|total|total_?count)$", re.I)


def ignored_params(path: str, data: Any) -> str:
    """개수를 줄이는 쿼리(limit=10 등)를 보냈는데 목록이 그보다 많이 오면, 서버가 모르는 파라미터라 무시한 것 (QA S-10)."""
    found = main_list(data)
    query = path.partition("?")[2]
    if not found or not query:
        return ""
    items = found[1]
    page = ", ".join(f"{k}={v}" for k, v in data.items() if _PAGE_FIELD.match(k) and isinstance(v, (int, str))) \
        if isinstance(data, dict) else ""
    notes = []
    for kv in query.split("&"):
        k, _, v = kv.partition("=")
        if _COUNT_PARAM.match(k) and v.isdigit() and 0 < int(v) < len(items):
            hint = f" 응답의 페이지 정보: {page} — 그 이름의 파라미터로 개수를 줄여 보세요." if page else ""
            notes.append(f"⚠ '{k}={v}' 를 보냈는데 목록이 {len(items)}개 왔습니다 — 서버가 '{k}' 를 모르는 파라미터로 무시한 것 "
                         f"같습니다 (문서에 없는 파라미터).{hint}")
    return "\n".join(notes)


def ignored_filters(path: str, data: Any) -> str:
    """'필드=값' 으로 걸렀는데 목록에 다른 값이 섞여 오면, 서버가 모르는 파라미터라 무시한 것 (QA S-10: 문서에 없는 file_name 등)."""
    found = main_list(data)
    query = path.partition("?")[2]
    if not found or not query:
        return ""
    items = [i for i in found[1] if isinstance(i, dict)]
    notes = []
    for kv in query.split("&"):
        k, _, raw = kv.partition("=")
        k, v = unquote(k), unquote(raw)
        if not k or not v or _COUNT_PARAM.match(k) or _PAGE_FIELD.match(k) or _PAGING.search(f"?{k}="):
            continue
        same = {v.lower(), unquote_plus(raw).lower()}   # '+' 는 글자 그대로일 수도, 띄어쓰기일 수도 있다
        vals = [str(i[k]) for i in items if k in i and not isinstance(i.get(k), (dict, list))]
        other = [x for x in dict.fromkeys(vals) if x.lower() not in same]
        if len(vals) >= 2 and other:
            notes.append(f"⚠ '{k}={v[:40]}' 로 걸렀는데 다른 값({', '.join(o[:24] for o in other[:3])})이 섞여 왔습니다 — 서버가 '{k}' 를 "
                         "모르는 파라미터로 무시했을 수 있습니다 (문서에 없는 파라미터). 이 목록을 그 조건의 결과로 쓰지 마세요.")
    return "\n".join(notes)


def _null_counts(counts: str) -> list[str]:
    """count_by 결과 중 값이 모두 비어 있거나(null·빈 글자) 응답에 아예 없는 필드 이름."""
    return [m.group(1) for m in re.finditer(r"^집계 \(([^)]+)\): (?:(?:null|) → \d+개 \(합 \d+개, 서로 다른 값 1가지\)|응답에 이 필드가 없음)",
                                            counts, re.M)]


def _null_count_note(api: ApiEndpoint, path: str, fields: list[str]) -> str:
    """모두 비어 있는 필드로 세려 하면: 이 응답으로는 셀 수 없고, 항목마다의 상세 경로에서 세라고 문서 후보와 함께 (QA S-15: 목록의
    label_category_id(모두 null)·메타 문자열로 '라벨'을 셈 — 항목별 라벨 상세는 1장만 조회)."""
    from agent import openapi
    base = path.split("?")[0].rstrip("/")
    template = next((m.group(2).split("?")[0].rstrip("/") for line in api.docs.splitlines()
                     if (m := openapi.ENDPOINT_LINE.match(line)) and m.group(1) == "GET" and _template(m.group(2).split("?")[0]).match(base)), "")
    items = []
    for line in api.docs.splitlines():
        m = openapi.ENDPOINT_LINE.match(line)
        if template and m and m.group(1) == "GET" and m.group(2).startswith(template + "/{") and not image_store.is_image_endpoint(api.docs, m.group(2)):
            items.append(line)
    items.sort(key=lambda x: not re.search(r"annotation|label|mark|detail|meta", x, re.I))
    listed = "\n".join(f"  {line[:150]}" for line in items[:4])
    return (f"⚠ '{', '.join(fields)}' 값이 이 응답에서 모두 비어 있거나 없습니다 — 이 필드로는 셀 수 없습니다. 항목마다 세라는 것(라벨 등)은 그 항목의 "
            "상세 결과로 셉니다 (모든 항목을 같은 경로로 조회)" + (f" — 항목별 경로 후보:\n{listed}" if listed else ".") +
            "\n다른 개념(메타 문자열 등)을 대신 쓰면 답에 그렇다고 밝히세요.")


_STATS = "count counts stats statistic summary total aggregate distribution breakdown"


def _empty_list_note(ctx: ToolContext, api: ApiEndpoint, path: str, data: Any) -> str:
    """빈 목록이면, '없다' 고 단정하기 전에 같은 대상을 다른 근거(집계)로 확인하라고 알려 준다 (QA S-12: 리뷰 목록이 비자
    'NG 이미지 없음' 으로 단정 — 실제 290장). 같은 경로에는 한 번만."""
    found = main_list(data)
    if not found or found[1] or (isinstance(data, dict) and any(_PAGE_FIELD.match(k) and k.lower() == "total"
                                                                 and isinstance(v, int) and v > 0 for k, v in data.items())):
        return ""
    base = path.split("?")[0]
    hinted = ctx.memo.setdefault("empty_hinted", set())
    if base in hinted:
        return ""
    hinted.add(base)
    from agent import openapi
    head = [s for s in base.strip("/").split("/")][:2]
    lines = [line for line in openapi.search_docs(api.docs, _STATS, limit=20) if line.startswith("GET ")]
    near = [line for line in lines if line.split()[1].strip("/").split("/")[:1] == head[:1]] or lines
    listed = "\n".join(f"  {line[:150]}" for line in near[:4])
    return ("ℹ 빈 목록입니다 (0개). '없다'고 결론 내기 전에 이 경로가 사용자가 말한 것과 같은 개념인지(예: 상태값과 다른 기록) 보고, "
            "같은 대상을 다른 근거로 확인하세요" + (f" — 집계 경로 후보:\n{listed}" if listed else ".") +
            "\n해석이 불확실하면 답에 그렇다고 밝히세요.")


_INDEX_KEYS = ("file_name", "filename", "name", "display_name", "title", "code", "key", "path", "id")
INDEX_CHARS = 1600


def item_index(data: Any) -> str:
    """긴 목록 응답의 항목 이름을 응답 순서대로 (잘려서 안 보이는 항목의 이름을 지어내지 않게 — QA S-8)."""
    found = main_list(data)
    if not found:
        return ""
    where, items = found
    dicts = [i for i in items if isinstance(i, dict)]
    if len(dicts) < 4:
        return ""
    key = next((k for k in _INDEX_KEYS if sum(isinstance(d.get(k), (str, int)) and d.get(k) != "" for d in dicts)
                >= 0.9 * len(dicts)), "")
    if not key:
        return ""
    kind = _category_key(dicts, key)
    shown, size = [], 0
    for i, d in enumerate(dicts, 1):
        piece = f"{i}) {d.get(key, '')}" + (f" [{kind}={d.get(kind)}]" if kind else "")
        if size + len(piece) > INDEX_CHARS:
            break
        shown.append(piece)
        size += len(piece) + 3
    more = f"\n  … 외 {len(dicts) - len(shown)}개 (이름이 필요하면 offset 으로 이어 읽기)" if len(shown) < len(dicts) else ""
    tail = (f" · [{kind}=…] 는 항목마다의 값 — '조건에 맞는 처음 N개'는 그 조건으로 거른 목록(쿼리)이나 이 값으로 앞에서부터 고른다"
            if kind else "")
    return (f"목록 색인 — {where or '목록'}[] {len(dicts)}개의 {key} (응답 순서 그대로, 도구가 응답 전체에서 뽑음{tail}):\n  "
            + "\n  ".join(shown) + more)


def _category_key(dicts: list[dict[str, Any]], skip: str) -> str:
    """항목을 나누는 값(상태·종류처럼 서로 다른 값이 2~8가지)인 필드 하나. 없으면 빈 글자 (QA IMG-1: 상태 필터 없는 목록에서
    '불량 처음 3장'을 고르다 순서가 어긋남 — 색인에 항목마다의 상태를 붙여 앞에서부터 고를 수 있게)."""
    best: tuple[int, int, str] | None = None
    for k in {k for d in dicts for k in d}:
        if k == skip or _ID_LIKE.search(k):
            continue
        vals = [d.get(k) for d in dicts if isinstance(d.get(k), (str, int, bool)) and not isinstance(d.get(k), float)]
        if len(vals) < 0.9 * len(dicts) or any(isinstance(v, str) and (len(v) > 20 or grounding.is_identifier(v)) for v in vals):
            continue
        distinct = len({str(v) for v in vals})
        if 2 <= distinct <= 8 and (best is None or (distinct, len(k)) < best[:2]):
            best = (distinct, len(k), k)
    return best[2] if best else ""


def _made_up_message(ctx: ToolContext, values: list[str]) -> str:
    listed = ", ".join(f"'{v[:80]}'" for v in values[:3])
    partial = ctx.memo.get("api_partial") or {}
    more = ""
    if partial:
        p, total, end = list(partial.values())[-1]
        more = (f" 앞서 받은 'GET {p}' 응답은 전체 {total}자 중 {end}자까지만 봤습니다 — 필요한 항목이 그 뒤에 있으면 같은 path 에 "
                f"offset={end} 로 이어 읽으세요.")
    return (f"이번 실행의 결과에 없던 값입니다: {listed} (보내지 않음). 파일명·ID 같은 식별자는 지어내거나 비슷하게 바꾸지 말고, "
            f"받은 결과에 실제로 있는 값을 그대로 쓰세요.{more} 목록이 길면 개수·범위를 줄이는 쿼리로 다시 받으세요.")


@tool("read")
def find_endpoint(
    ctx: ToolContext,
    query: Annotated[str, Field(description="찾을 기능의 영어 키워드 (띄어쓰기로 여러 개). 예) dataset upload")],
    api: Annotated[str, Field(description="등록된 API 이름. 비우면 전체에서 찾는다")] = "",
) -> str:
    """등록된 사내 API 의 전체 엔드포인트 목록에서 키워드로 경로를 찾는다. 엔드포인트가 많아 목록에 다 안 보일 때 쓴다."""
    from agent import openapi

    apis = [find_api(ctx.settings, api)] if api else ctx.settings.apis
    if not apis:
        raise ToolError("등록된 API 가 없습니다. Swagger 문서 주소를 받아 add_api 로 먼저 등록하세요")
    lines = []
    for a in apis:
        ensure_params(ctx, a)
        lines += [f"[{a.name}] {line}" for line in openapi.search_docs(a.docs, query)]
    if not lines:
        return f"'{query}' 와 맞는 엔드포인트가 없습니다. 다른 영어 키워드(동의어, 단수/복수)로 다시 찾아보세요."
    source = [n for n, s in (ctx.memo.get("params_source") or {}).items() if any(a.name == n for a in apis)]
    note = " 쿼리 파라미터(?a&b)는 API 서버의 명세에서 읽었다 — 등록한 문서에는 없음" if source else ""
    return "\n".join(lines[:30]) + f"\n(* = 필수. {{이름}} 은 실제 값으로 바꿔 부른다.{note})"


SPEC_TTL = 600   # API 서버 명세에서 읽은 파라미터를 다시 쓰는 시간(초)
_SPEC_PARAMS: dict[str, tuple[float, dict[str, str] | None]] = {}


def ensure_params(ctx: ToolContext, api: ApiEndpoint) -> None:
    """등록한 문서에 쿼리 파라미터 정보가 하나도 없으면(저장한 Swagger 화면 등), API 서버가 내주는 명세를 한 번 보고 그 파라미터를
    이번 실행의 문서 줄에 붙인다 (QA S-10: 문서에 없는 limit·file_name·offset 이 그대로 보내졌다). 명세를 못 받으면 그대로 둔다 —
    그때는 응답을 보고 무시된 파라미터를 알린다 (ignored_params · ignored_filters)."""
    from agent import openapi
    flag = f"params:{api.name}"
    if ctx.memo.get(flag):
        return
    ctx.memo[flag] = True
    gets = [m.group(2) for line in api.docs.splitlines() if (m := openapi.ENDPOINT_LINE.match(line)) and m.group(1) == "GET"]
    if api.docs_url or len(gets) < 5 or any("?" in g for g in gets):
        return   # 서버 명세 주소로 등록했으면 문서가 이미 그 명세에서 왔다 (파일·직접 적은 문서만 본다)
    key = f"{api.name}|{api.base_url}"
    hit = _SPEC_PARAMS.get(key)
    if hit is None or time.monotonic() - hit[0] > SPEC_TTL:
        hit = (time.monotonic(), _server_params(ctx, api))
        _SPEC_PARAMS[key] = hit
    params = hit[1]
    if not params:
        return
    out = []
    for line in api.docs.splitlines():
        m = openapi.ENDPOINT_LINE.match(line)
        if m and "?" not in m.group(2) and (q := params.get(f"{m.group(1)} {m.group(2).rstrip('/')}")):
            line = f"{m.group(1)} {m.group(2)}?{q}" + line[m.end():]
        out.append(line)
    api.docs = "\n".join(out)
    ctx.memo.setdefault("params_source", {})[api.name] = "API 서버의 명세"


def _server_params(ctx: ToolContext, api: ApiEndpoint) -> dict[str, str] | None:
    """API 기본 주소에서 OpenAPI 명세를 찾아 '메서드 경로' → 'a&b*' (없거나 못 받으면 None). 인증은 서버 쪽 헤더로만."""
    from agent import openapi
    base = api.base_url.rstrip("/")
    for cand in openapi.SPEC_CANDIDATES:
        try:
            url = f"{base}/{cand}"
            check_url(ctx.settings, url)
            r, _, _ = exchange(ctx.settings, api, "GET", url, timeout=8)
        except ToolError as e:
            if "API 호출 실패" in str(e):   # 연결이 안 되면 다른 후보도 안 된다
                return None
            continue
        if r.status_code != 200:
            continue
        try:
            spec = json.loads(r.content)
        except ValueError:
            continue
        if openapi.is_spec(spec):
            return {f"{e['method']} {e['path'].rstrip('/')}": "&".join(e["query"]) for e in openapi.endpoints(spec) if e["query"]}
    return None


OPAQUE_MIN = 200   # 띄어쓰기 없는 이보다 긴 글자(RLE·base64·해시)는 줄인다 — 읽기·세기를 방해하고 컨텍스트만 먹는다 (QA S-1)


def shorten_opaque(obj: Any) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= OPAQUE_MIN or " " in obj[:OPAQUE_MIN] else f"{obj[:24]}…({len(obj)}자 생략)"
    if isinstance(obj, list):
        return [shorten_opaque(x) for x in obj]
    if isinstance(obj, dict):
        return {k: shorten_opaque(v) for k, v in obj.items()}
    return obj


def count_block(data: Any, fields: str) -> str:
    """응답 전체에서 필드 값별 개수를 정확히 센다 (QA S-1: 머릿속 집계가 뒤바뀜). 어느 깊이에 있든 같은 이름의 필드를 센다."""
    names = [f.strip() for f in fields.split(",") if f.strip()]
    found: dict[str, dict[str, int]] = {n: {} for n in names}

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if k in found and not isinstance(v, (dict, list)):
                    key = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    found[k][key] = found[k].get(key, 0) + 1
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    out = []
    for n, vals in found.items():
        if not vals:
            out.append(f"집계 ({n}): 응답에 이 필드가 없음")
            continue
        ranked = sorted(vals.items(), key=lambda kv: -kv[1])
        shown = ", ".join(f"{v} → {c}개" for v, c in ranked[:30]) + (f" … 외 {len(ranked) - 30}가지" if len(ranked) > 30 else "")
        out.append(f"집계 ({n}): {shown} (합 {sum(vals.values())}개, 서로 다른 값 {len(vals)}가지) — 도구가 센 정확한 값")
    return "\n".join(out)


_ID_LIKE = re.compile(r"^(id|uuid|guid|key|name|title|file_?name|path|url|href|md5|sha\d*|hash|checksum|token)$|"
                      r"(_at|_time|_date|date|time|stamp)$", re.I)


def auto_counts(data: Any, limit: int = 4) -> str:
    """객체 목록 안에서 값이 되풀이되는 필드(종류 id·상태 등)를 도구가 미리 센다 — 모델이 count_by 를 고르지 않아도
    정확한 개수가 결과 맨 앞에 있게 (QA S-1: 실제 모델이 6개 중 4개인 값을 3개로 셌다)."""
    found = auto_count_data(data)[:limit]
    if not found:
        return ""
    lines = [f"{label}: {_fmt(c)} (합 {sum(c.values())}개)" for _, label, c in found]
    return "자동 집계 (도구가 센 정확한 개수 — 머리로 다시 세지 말 것):\n" + "\n".join(f"  {t}" for t in lines)


def _fmt(counts: dict[str, int]) -> str:
    return ", ".join(f"{v} → {c}개" for v, c in sorted(counts.items(), key=lambda kv: -kv[1]))


def running_totals(ctx: ToolContext, api: ApiEndpoint, path: str, data: Any) -> str:
    """같은 엔드포인트를 다른 대상으로 여러 번 받으면 자동 집계의 누계도 알려 준다 (QA S-1: 여러 응답을 합치는 계산이 틀렸다)."""
    from agent import openapi
    base = path.split("?")[0]
    gets = [m.group(2).split("?")[0] for line in api.docs.splitlines()
            if (m := openapi.ENDPOINT_LINE.match(line)) and m.group(1) == "GET"]
    template = next((g for g in gets if "{" in g and _template(g).match(base)), "")
    if not template:
        return ""
    store = ctx.memo.setdefault("api_totals", {})
    seen = ctx.memo.setdefault("api_total_paths", {}).setdefault((api.name, template), [])   # 이 틀로 받은 응답 (순서대로)
    if base not in seen:
        seen.append(base)
    lines = []
    for _, label, counts in auto_count_data(data)[:4]:
        entry = store.setdefault((api.name, template, label), {"paths": [], "counts": {}})
        if base not in entry["paths"]:
            entry["paths"].append(base)
            for v, c in counts.items():
                entry["counts"][v] = entry["counts"].get(v, 0) + c
        if len(entry["paths"]) >= 2:
            lines.append(f"  {label}: {_fmt(entry['counts'])} (합 {sum(entry['counts'].values())}개)")
    if not lines:
        return ""
    ids = ", ".join(_varying(template, p) for p in seen[-12:]) + (f" 외 {len(seen) - 12}개" if len(seen) > 12 else "")
    return (f"누계 (여러 응답을 합친 값 — 이 응답 하나의 값이 아님): 이번 실행에서 GET {template} 로 받은 응답 {len(seen)}개 "
            f"({ids}) 를 합친 정확한 개수 (그 필드가 없는 응답은 0개로 침):\n" + "\n".join(lines))


def _varying(template: str, path: str) -> str:
    """틀의 마지막 {자리}에 들어간 값 (누계에 어느 대상들을 합쳤는지 보이게). 길면 앞부분만."""
    parts, given = template.rstrip("/").split("/"), path.rstrip("/").split("/")
    slots = [i for i, p in enumerate(parts) if p.startswith("{")]
    value = unquote(given[slots[-1]]) if slots and slots[-1] < len(given) else given[-1]
    return value if len(value) <= 28 else value[:26] + "…"


def auto_count_data(data: Any) -> list[tuple[float, str, dict[str, int]]]:
    found: list[tuple[float, str, dict[str, int]]] = []

    def walk(x: Any, where: str) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, f"{where}.{k}" if where else k)
        elif isinstance(x, list):
            items = [i for i in x if isinstance(i, dict)]
            if items:
                for key in {k for i in items for k in i}:
                    if _ID_LIKE.search(key):
                        continue   # 고유한 id·이름·시각은 세지 않는다 (종류 id 처럼 '…_id' 는 센다)
                    vals = [i[key] for i in items if key in i and isinstance(i[key], (str, int, bool))
                            and not (isinstance(i[key], str) and (len(i[key]) > 40 or grounding.is_identifier(i[key])))]
                    # UUID 같은 값은 부모를 가리키는 참조라 종류별 개수가 아니다 (자동 집계 자리를 차지하지 않게)
                    distinct: dict[str, int] = {}
                    for v in vals:
                        distinct[str(v)] = distinct.get(str(v), 0) + 1
                    small = len(items) <= 5   # 짧은 목록은 되풀이가 없어도 센다 (표시 1~2개짜리 항목도 합계에 들어가야 한다)
                    if len(vals) >= max(1, len(items) // 2) and 1 <= len(distinct) <= 12 and (small or len(distinct) < len(vals)):
                        found.append((len(distinct) / len(vals), f"{where or '목록'}[].{key}", distinct))
            for i in x:
                walk(i, f"{where}[]" if where else "[]")

    walk(data, "")
    return sorted(found, key=lambda t: (t[0], t[1]))


def _template(path: str) -> re.Pattern:
    parts = re.split(r"\{[^}]*\}", path.rstrip("/"))
    return re.compile("^" + "[^/]+".join(map(re.escape, parts)) + "/?$")


def check_documented(api: ApiEndpoint, path: str, source: str = "") -> None:
    """문서(명세·저장 화면)로 등록한 API 에 문서에 없는 경로를 지어내 부르면 서버에 보내지 않고 비슷한 경로를 알려 준다 (QA S-6)."""
    from agent import openapi
    documented = [m.group(2) for line in api.docs.splitlines()
                  if (m := openapi.ENDPOINT_LINE.match(line)) and m.group(1) == "GET"]
    base, _, query = path.partition("?")
    hits = [d for d in documented if _template(d.split("?")[0]).match(base)]
    if hits:
        _check_query(api, base, query, hits, source)
        return
    if len(documented) < 5:   # 사람이 적어 준 짧은 목록이면 판단하지 않는다
        return
    near = [line for line in openapi.search_docs(api.docs, " ".join(re.findall(r"[A-Za-z]+", base)), limit=8)
            if line.startswith("GET ")][:5]
    if not near:   # 낱말이 안 맞으면 가장 짧은(맨 위) 조회 경로들로 방향만 알려 준다
        tops = [line for line in api.docs.splitlines() if line.startswith("GET ")]
        near = sorted(tops, key=lambda x: (x.split()[1].count("/"), len(x.split()[1])))[:5]
    listed = "\n".join(f"  {line[:140]}" for line in near) or "  (비슷한 경로 없음)"
    raise ToolError(f"문서에 없는 경로입니다: GET {base} (보내지 않음). 경로를 지어내지 말고 문서에 있는 것을 쓰세요. 비슷한 경로:\n"
                    f"{listed}\n(더 찾으려면 find_endpoint(api='{api.name}', query='영어 키워드'))")


def _check_query(api: ApiEndpoint, base: str, query: str, hits: list[str], source: str = "") -> None:
    """문서(명세)에 쿼리 파라미터 목록이 있으면 그 밖의 이름은 보내지 않는다 — 서버는 모르는 파라미터를 조용히 무시해
    '10개만' 이 60개가 된다 (QA S-10). 문서에 파라미터 정보가 없으면(저장한 화면 등) 응답을 보고 알린다(ignored_params)."""
    if not query or not any("?" in h for h in hits):
        return
    known = {n.strip().rstrip("*") for h in hits if "?" in h for n in h.split("?", 1)[1].split("&") if n.strip()}
    sent = [kv.partition("=")[0] for kv in query.split("&") if kv]
    unknown = [n for n in dict.fromkeys(sent) if n and n not in known]
    if unknown:
        raise ToolError(f"문서에 없는 쿼리 파라미터입니다: {', '.join(unknown)} (보내지 않음 — 서버는 모르는 파라미터를 조용히 무시해 "
                        f"엉뚱한 결과가 옵니다). GET {hits[0].split('?')[0]} 의 문서 파라미터: {', '.join(sorted(known))}"
                        + (f" (파라미터 출처: {source})" if source else ""))


PAGE_CHARS = 3500        # 한 번에 돌려주는 응답 글자 수 (긴 응답은 offset 으로 이어 읽는다 — QA API-9)
PAGE_KEEP_SECONDS = 300
_PAGING = re.compile(r"[?&](page|offset|skip|start|cursor|from)=([^&]*)", re.I)
AGGREGATE_QUERY = "count stats statistic summary total aggregate distribution breakdown histogram"


def _aggregate_hint(ctx: ToolContext, api: ApiEndpoint, path: str, long: bool) -> str:
    """목록을 페이지마다 받아 세려 하면, 문서의 집계 엔드포인트 후보를 알려 준다 (QA API-8). 같은 경로에 한 번만."""
    base = path.split("?", 1)[0]
    pages = ctx.memo.setdefault("api_paged", {}).setdefault(f"{api.name}{base}", set())
    if m := _PAGING.search(path):
        pages.add(m.group(2))
    if not (len(pages) >= 2 or (long and pages)) or f"{api.name}{base}" in ctx.memo.setdefault("api_hinted", set()):
        return ""
    ctx.memo["api_hinted"].add(f"{api.name}{base}")
    from agent import openapi
    found = [line for line in openapi.search_docs(api.docs, AGGREGATE_QUERY, limit=12)
             if line.startswith("GET ") and line.split("?")[0].split()[1] != base][:6]
    listed = "\n".join(f"  {line[:160]}" for line in found) or "  (문서에서 못 찾음)"
    return ("\n\nℹ 목록을 페이지마다 받아 직접 세려는 것 같습니다. 개수·합계·분포를 물었다면 먼저 집계 엔드포인트로 답하세요 "
            f"(전수 조회는 집계 엔드포인트가 없을 때만). 문서에서 찾은 후보:\n{listed}\n"
            f"(더 찾으려면 find_endpoint(api='{api.name}', query='count stats summary …'))")


@tool("read", taints=True)
def api_get(
    ctx: ToolContext,
    api: Annotated[str, Field(description="등록된 API 이름 (시스템 프롬프트의 '사내 API' 목록 참고)")],
    path: Annotated[str, Field(description="'/' 로 시작하는 경로와 쿼리. 예) /lots/123?line=A1")],
    offset: Annotated[int, Field(description="긴 응답을 이어 읽을 때 몇 번째 글자부터 (같은 path 로)", ge=0)] = 0,
    count_by: Annotated[str, Field(description="응답 JSON 에서 값별 개수를 셀 필드 이름 (쉼표로 여러 개). 예) category_id — 머리로 세지 말고 이걸로 센다")] = "",
) -> str:
    """등록된 사내 API 에 GET 요청을 보내 결과(주로 JSON)를 돌려준다. 조회 전용. 긴 응답은 offset 으로 나눠 읽는다.
    이미지 응답은 채팅 화면에 붙이고 '붙였음'만 돌려준다."""
    cache = ctx.memo.setdefault("api_pages", {})
    key = f"{api}|{path}|{count_by}"
    hit = cache.get(key)
    if offset and hit and time.monotonic() - hit[0] < PAGE_KEEP_SECONDS:
        text = hit[1]
    else:
        a = find_api(ctx.settings, api)
        ensure_params(ctx, a)   # S-10: 문서에 파라미터 정보가 없으면 API 서버의 명세에서
        check_documented(a, path, (ctx.memo.get("params_source") or {}).get(a.name, ""))
        text = _call(ctx, api, "GET", path, count_by=count_by)
        cache[key] = (time.monotonic(), text)
        while len(cache) > 20:
            cache.pop(next(iter(cache)))
    failed = isinstance(text, Failed)
    hint = "" if offset or failed else _aggregate_hint(ctx, find_api(ctx.settings, api), path, len(text) > PAGE_CHARS)
    chunk = text[offset: offset + max(1500, PAGE_CHARS - len(hint))]   # 결과 전체가 루프의 자르기 한도(4천 자) 안에 들게
    end = offset + len(chunk)
    tail = ""
    partial = ctx.memo.setdefault("api_partial", {})   # S-8: 다 보지 못한 응답 (지어낸 값을 막을 때 이어 읽기를 안내)
    partial.pop(f"{api}|{path}", None)
    if end < len(text):
        partial[f"{api}|{path}"] = (path, len(text), end)
        tail = (f"\n\n(응답 전체 {len(text)}자 중 {offset}-{end}자. 이어 읽으려면 같은 path 로 offset={end}. "
                "안 보이는 항목의 값은 짐작해 쓰지 말 것. 목록이 길면 쿼리로 범위를 좁히거나 집계 엔드포인트를 쓰는 편이 낫다)")
    if offset:
        reads = ctx.memo.setdefault("api_reads", {})
        reads[key] = reads.get(key, 0) + 1
        if reads[key] == 3:   # 긴 목록을 쪽마다 훑는 중 (QA 재현: 없는 이름을 확인하려고 목록 전체를 15번 이어 읽음)
            tail += ("\n\nℹ 같은 응답을 여러 번 이어 읽고 있습니다. 찾는 항목의 이름·ID 를 알면 그 이름으로 바로 조회하세요 (없으면 404 로 바로 "
                     "압니다). 이름 목록은 첫 쪽의 '목록 색인'에, 개수·분포는 집계 경로에 있습니다.")
    elif offset:
        tail = f"\n\n(응답 끝, 전체 {len(text)}자)"
    out = chunk + tail + hint
    return Failed(out) if failed else out


@tool("send", taints=True)
def api_send(
    ctx: ToolContext,
    api: Annotated[str, Field(description="등록된 API 이름")],
    method: Annotated[Literal["POST", "PUT", "PATCH", "DELETE"], Field(description="HTTP 메서드")],
    path: Annotated[str, Field(description="'/' 로 시작하는 경로")],
    body: Annotated[str, Field(description="보낼 JSON 문자열. 파일 업로드면 함께 보낼 폼 필드. 없으면 비움")] = "",
    file_path: Annotated[str, Field(description="업로드할 파일 경로 (작업 폴더 기준). 문서에 'form(파일 업로드)' 인 경로에 쓴다")] = "",
    file_field: Annotated[str, Field(description="파일을 담을 폼 필드 이름. 문서의 form 항목 이름 (보통 file)")] = "file",
) -> str:
    """등록된 사내 API 에 데이터를 보내거나 바꾸는 요청을 한다 (파일 업로드 포함). 실행 전에 사람이 승인해야 한다."""
    return _call(ctx, api, method, path, body, file_path, file_field)
