"""Swagger / OpenAPI 문서 읽기 — 사내 API 의 엔드포인트 목록을 사람 대신 뽑아낸다.

FastAPI 의 /docs, Spring 의 /swagger-ui 같은 페이지는 자바스크립트로 그려져 본문이 비어 있다.
대신 그 페이지가 불러오는 명세(openapi.json)를 직접 받아 '메서드 경로 설명 + 파라미터' 목록으로 만든다.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from agent.config import Settings
from agent.tools.base import ToolError, decode_bytes

SPEC_CANDIDATES = ["openapi.json", "swagger.json", "v3/api-docs", "v2/api-docs", "swagger/v1/swagger.json", "api-docs"]
METHODS = ("get", "post", "put", "patch", "delete")
_SPEC_IN_HTML = [
    re.compile(r"""\burl\s*:\s*['"]([^'"]+)['"]"""),         # SwaggerUIBundle({ url: '/openapi.json' })
    re.compile(r"""spec-url\s*=\s*['"]([^'"]+)['"]"""),      # <redoc spec-url="...">
    re.compile(r"""['"]([^'"\s]*(?:openapi|swagger|api-docs)[^'"\s]*)['"]"""),
]
MAX_DOC_LINES = 2000   # 전부 저장한다. 시스템 프롬프트에는 일부만 넣고 나머지는 find_endpoint 로 찾는다


def looks_like_docs_page(html: str) -> bool:
    low = html[:30000].lower()
    return any(k in low for k in ("swagger-ui", "swaggeruibundle", "<redoc", "redoc.standalone"))


def is_spec(data: Any) -> bool:
    return isinstance(data, dict) and ("openapi" in data or "swagger" in data) and isinstance(data.get("paths"), dict)


def candidate_spec_urls(url: str, html: str | None = None) -> list[str]:
    out: list[str] = []
    if url.lower().split("?")[0].endswith(".json"):
        out.append(url)
    for rx in _SPEC_IN_HTML:
        for found in rx.findall(html or ""):
            low = found.lower()
            if any(k in low for k in (".json", "api-docs", "openapi", "swagger")) and not low.endswith((".js", ".css")):
                out.append(urljoin(url, found))
    p = urlsplit(url)
    folder = url.split("?")[0].rsplit("/", 1)[0] + "/"
    for base in dict.fromkeys([folder, f"{p.scheme}://{p.netloc}/"]):   # 문서가 있는 폴더 기준, 사이트 맨 위 기준
        out += [urljoin(base, c) for c in SPEC_CANDIDATES]
    return list(dict.fromkeys(out))


def load_spec(settings: Settings, url: str, headers: dict[str, str] | None = None, verify: bool = True,
              html: str | None = None) -> tuple[str, dict]:
    """url 이 문서 페이지든 JSON 이든 OpenAPI 명세를 찾아 (명세 주소, 명세) 를 돌려준다."""
    from agent.tools.web import _download   # web 이 이 모듈을 쓰므로 순환 import 를 피한다

    if html is None and not url.lower().split("?")[0].endswith(".json"):
        try:
            final, _, raw = _download(settings, url, headers, verify)
            text = decode_bytes(raw)
            if is_spec(_json(text)):
                return final, _json(text)
            html, url = text, final
        except ToolError:
            pass
    for cand in candidate_spec_urls(url, html):
        try:
            final, _, raw = _download(settings, cand, headers, verify)
        except ToolError:
            continue
        spec = _json(decode_bytes(raw))
        if is_spec(spec):
            return final, spec
    raise ToolError("OpenAPI(Swagger) 명세를 찾지 못했습니다. /openapi.json 같은 명세 주소를 직접 알려주세요")


# ───────────────────────── 저장한 문서 파일 (QA API-14: 서버 문서가 안 열려 사용자가 저장한 HTML 만 있을 때)

_SW_TAG = re.compile(r'<h3[^>]*class="opblock-tag[^"]*"[^>]*data-tag="([^"]*)"')
_SW_OP = re.compile(r'<div class="opblock opblock-(get|post|put|patch|delete)\b')
_SW_PATH = re.compile(r'data-path="([^"]*)"')
_SW_DESC = re.compile(r'<div class="opblock-summary-description">(.*?)</div>', re.S)
_SW_TITLE = re.compile(r'<h[12] class="title">(.*?)<(?:span|small)', re.S)
_SW_VERSION = re.compile(r'<pre class="version">\s*([^<]*?)\s*</pre>')
SCREEN_LOCK = "화면의 자물쇠"


def _plain(html_text: str) -> str:
    import html as _html
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", html_text))).strip()


def spec_from_swagger_html(page: str) -> dict | None:
    """브라우저로 그려진 뒤 저장한 Swagger UI 화면에서 메서드·경로·요약·태그·자물쇠를 읽어 최소 명세를 만든다.
    (저장 화면에는 파라미터·인증 방식 상세가 없다 — 펼쳐 둔 것만 있다.)"""
    ops = list(_SW_OP.finditer(page))
    if not ops:
        return None
    tags = [(m.start(), m.group(1)) for m in _SW_TAG.finditer(page)]
    paths: dict[str, dict[str, Any]] = {}
    locked = 0
    for k, m in enumerate(ops):
        block = page[m.start(): ops[k + 1].start() if k + 1 < len(ops) else len(page)]
        p = _SW_PATH.search(block)
        if not p:
            continue
        desc = _SW_DESC.search(block)
        tag = next((t for pos, t in reversed(tags) if pos < m.start()), "")
        lock = "authorization__btn" in block[:4000]
        locked += lock
        paths.setdefault(_plain(p.group(1)), {})[m.group(1)] = {
            "summary": _plain(desc.group(1)) if desc else "", "tags": [tag] if tag else [],
            "security": [{SCREEN_LOCK: []}] if lock else []}
    if not paths:
        return None
    title = _SW_TITLE.search(page)
    version = _SW_VERSION.search(page)
    spec: dict[str, Any] = {"openapi": "3.0.0", "x-source": "saved-swagger-ui",
                            "info": {"title": _plain(title.group(1)) if title else "", "version": version.group(1) if version else ""},
                            "paths": paths}
    if locked:
        spec["components"] = {"securitySchemes": {SCREEN_LOCK: {"type": "screen-lock"}}}
    return spec


def load_local(path: "Any") -> dict | None:
    """작업 폴더의 문서 파일 → 명세. OpenAPI JSON 이나 저장한 Swagger UI 화면(HTML)이면 명세, 아니면 None."""
    from pathlib import Path as _P
    raw = _P(path).read_bytes()
    text = decode_bytes(raw)
    data = _json(text)
    if is_spec(data):
        return data
    return spec_from_swagger_html(text) if "<" in text[:2000] else None


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def base_url(spec: dict, spec_url: str) -> str:
    """API 를 부를 기본 주소. 명세의 servers(3.x) / host+basePath(2.0), 없으면 명세가 있던 서버."""
    p = urlsplit(spec_url)
    origin = f"{p.scheme}://{p.netloc}"
    servers = spec.get("servers") or []
    if servers and isinstance(servers[0], dict) and servers[0].get("url"):
        return urljoin(spec_url, servers[0]["url"]).rstrip("/") or origin
    if spec.get("swagger"):
        scheme = (spec.get("schemes") or [p.scheme])[0]
        return f"{scheme}://{spec.get('host') or p.netloc}{spec.get('basePath', '')}".rstrip("/")
    return origin


def _resolve(spec: dict, node: Any, depth: int = 0) -> Any:
    while isinstance(node, dict) and "$ref" in node and depth < 10:
        target: Any = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        node, depth = target, depth + 1
    return node


def _fields(spec: dict, schema: Any) -> list[str]:
    schema = _resolve(spec, schema)
    if not isinstance(schema, dict):
        return []
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            return [f for sub in schema[key] for f in _fields(spec, sub)]
    required = set(schema.get("required") or [])
    out = []
    for name, prop in (schema.get("properties") or {}).items():
        prop = _resolve(spec, prop)
        kind = "파일" if isinstance(prop, dict) and prop.get("format") == "binary" else ""
        out.append(name + ("*" if name in required else "") + (f"({kind})" if kind else ""))
    return out


def endpoints(spec: dict) -> list[dict[str, Any]]:
    out = []
    for path, item in (spec.get("paths") or {}).items():
        item = _resolve(spec, item)
        if not isinstance(item, dict):
            continue
        common = item.get("parameters") or []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = [_resolve(spec, p) for p in common + (op.get("parameters") or [])]
            query = [p["name"] + ("*" if p.get("required") else "") for p in params
                     if isinstance(p, dict) and p.get("in") == "query" and p.get("name")]
            body, form = [], []
            for p in params:   # Swagger 2.0 의 body / formData
                if isinstance(p, dict) and p.get("in") == "body":
                    body += _fields(spec, p.get("schema"))
                elif isinstance(p, dict) and p.get("in") == "formData":
                    form.append(p["name"] + ("*" if p.get("required") else "") + ("(파일)" if p.get("type") == "file" else ""))
            content = (_resolve(spec, op.get("requestBody")) or {}).get("content") or {}
            for ctype, media in content.items():   # OpenAPI 3.x 의 requestBody
                (form if "form" in ctype else body).extend(_fields(spec, (media or {}).get("schema")))
            summary = op.get("summary") or (op.get("description") or "").strip().split("\n")[0]
            out.append({"method": method.upper(), "path": path, "summary": summary[:80], "query": query,
                        "body": list(dict.fromkeys(body)), "form": list(dict.fromkeys(form)),
                        "tag": (op.get("tags") or [""])[0]})
    return out


def endpoint_line(e: dict[str, Any]) -> str:
    line = f"{e['method']} {e['path']}"
    if e["query"]:
        line += "?" + "&".join(e["query"])
    if e["summary"]:
        line += f"  {e['summary']}"
    if e["body"]:
        line += "  body: {" + ", ".join(e["body"][:12]) + "}"
    if e["form"]:
        line += "  form(파일 업로드): {" + ", ".join(e["form"][:12]) + "}"
    return line[:220]


ENDPOINT_LINE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/\S*)")


def path_groups(docs: str, top: int = 15) -> str:
    """엔드포인트가 많을 때 시스템 프롬프트에 넣을 요약: 첫 경로 조각별 개수. 예) /datasets(35) · /images(20)"""
    counts: dict[str, int] = {}
    for line in docs.splitlines():
        m = ENDPOINT_LINE.match(line)
        if m:
            group = "/" + m.group(2).split("?")[0].strip("/").split("/")[0]
            counts[group] = counts.get(group, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    text = " · ".join(f"{g}({n})" for g, n in ranked[:top])
    return text + (f" · 외 {len(ranked) - top}개 그룹" if len(ranked) > top else "")


_STOP = {"the", "a", "an", "of", "for", "to", "by", "in", "on", "and", "or", "with", "api", "endpoint"}


def _stem(term: str) -> str:
    """복수형만 단순히 접는다 (labels→label, categories→category, stats→stat)."""
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def search_docs(docs: str, query: str, limit: int = 25) -> list[str]:
    """엔드포인트 줄을 검색어로 고른다 (QA API-12: 여러 키워드 질의에서 일부만 맞는 엔드포인트가 사라졌다).

    - 드문 낱말일수록 무겁게 (흔한 'image' 보다 드문 'label' 이 맞는 줄이 위로) · 경로에 있으면 더 무겁게
    - 복수형을 접어 맞춘다 · 낱말마다 그 낱말이 맞는 상위 몇 줄은 결과에 꼭 남긴다
    """
    lines = [line for line in docs.splitlines() if ENDPOINT_LINE.match(line)]
    words = [_stem(w) for w in re.findall(r"[a-z0-9]+", query.lower())]
    terms = list(dict.fromkeys(w for w in words if w not in _STOP)) or list(dict.fromkeys(words))
    if not lines or not terms:
        return []
    toks = [{_stem(w) for w in re.findall(r"[a-z0-9]+", x.lower())} for x in lines]   # 낱말 단위 ('stat' ≠ 'status')
    path_toks = [{_stem(w) for w in re.findall(r"[a-z0-9]+", x.split("  ", 1)[0].lower())} for x in lines]

    def hit(t: str, i: int) -> float:
        if t in toks[i]:
            return 1.0
        return 0.4 if len(t) >= 4 and any(w.startswith(t) for w in toks[i]) else 0.0   # labeling ⊃ label 은 약하게

    idf = {t: math.log((len(lines) + 1) / (sum(t in x for x in toks) + 1)) + 1 for t in terms}

    def score(i: int) -> float:
        return sum(idf[t] * hit(t, i) * (1.5 if t in path_toks[i] else 1.0) for t in terms)

    scores = {i: s for i in range(len(lines)) if (s := score(i)) > 0}
    ranked = sorted(scores, key=lambda i: (-scores[i], len(lines[i])))
    per_term = max(3, limit // (2 * len(terms)))
    keep: list[int] = []
    for t in terms:   # 낱말마다 상위 몇 줄은 꼭 (결합 질의 때문에 관련 줄이 밀려나지 않게)
        keep += [i for i in ranked if t in toks[i]][:per_term]
    chosen = set(keep)
    for i in ranked:
        if len(chosen) >= max(limit, len(set(keep))):
            break
        chosen.add(i)
    return [lines[i] for i in ranked if i in chosen]


def docs_text(spec: dict) -> str:
    """add_api 의 docs 칸에 넣을 엔드포인트 목록 (모델이 보고 따라 쓴다)."""
    eps = endpoints(spec)
    lines = [endpoint_line(e) for e in eps[:MAX_DOC_LINES]]
    if len(eps) > MAX_DOC_LINES:
        lines.append(f"… 외 {len(eps) - MAX_DOC_LINES}개")
    lines.append("(* = 필수. path 의 {이름} 은 실제 값으로 바꿔 부른다)")
    return "\n".join(lines)


def tag_overview(eps: list[dict[str, Any]]) -> str:
    """기능 그룹(태그, 없으면 첫 경로 조각)별 엔드포인트 수 — 전부 (긴 문서의 첫 쪽에서 전체를 보게, QA API-3)."""
    counts: dict[str, int] = {}
    for e in eps:
        group = e["tag"] or "/" + e["path"].strip("/").split("/")[0]
        counts[group] = counts.get(group, 0) + 1
    return " · ".join(f"{g} {n}" for g, n in sorted(counts.items(), key=lambda kv: -kv[1]))


# ───────────────────────── 인증 정의 (QA API-2: 요약에서 인증 정보가 빠졌다)

_LOCAL = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _is_local(host: str) -> bool:
    return host.lower() in _LOCAL or host.lower().endswith(".localhost") or host.startswith("127.")


def fix_local_url(url: str, spec_url: str) -> tuple[str, bool]:
    """문서의 주소가 API 서버 자신(localhost)을 가리키면, 문서를 받은 호스트로 바꾼 주소. (주소, 바꿨는지)."""
    u, s = urlsplit(url), urlsplit(spec_url)
    if u.hostname and _is_local(u.hostname) and s.hostname and not _is_local(s.hostname):
        netloc = s.hostname + (f":{u.port}" if u.port else "")
        return u._replace(netloc=netloc).geturl(), True
    return url, False


def security_info(spec: dict, spec_url: str) -> dict[str, Any]:
    """문서에 정의된 인증 방식과 쓰임새. schemes: [{name, type, how, header, login_url, doc_login_url, local}]"""
    comps = spec.get("components") or {}
    raw = comps.get("securitySchemes") or spec.get("securityDefinitions") or {}
    schemes = []
    for name, sch in raw.items():
        sch = _resolve(spec, sch)
        if not isinstance(sch, dict):
            continue
        item: dict[str, Any] = {"name": name, "type": sch.get("type", ""), "how": "", "header": "Authorization",
                                "login_url": "", "doc_login_url": "", "local": False}
        typ = item["type"]
        flows = sch.get("flows") or {}
        if typ == "oauth2" and ("password" in flows or sch.get("flow") == "password"):
            doc = (flows.get("password") or {}).get("tokenUrl") or sch.get("tokenUrl") or ""
            full = urljoin(spec_url, doc) if doc else ""
            fixed, local = fix_local_url(full, spec_url) if full else ("", False)
            item.update(how="login", doc_login_url=full, login_url=fixed, local=local)
        elif typ == "oauth2":
            item["how"] = "oauth2-other"
        elif typ == "http" and str(sch.get("scheme", "")).lower() == "bearer" or typ == "apiKey" and sch.get("in") == "header":
            item.update(how="token", header=sch.get("name", "Authorization") if typ == "apiKey" else "Authorization")
        elif typ == "http" and str(sch.get("scheme", "")).lower() == "basic":
            item["how"] = "basic"
        elif typ == "screen-lock":   # 저장한 Swagger 화면: 자물쇠만 보이고 방식은 모른다
            item["how"] = "screen"
        else:
            item["how"] = "other"
        schemes.append(item)
    top = spec.get("security")
    total, needed, public = 0, 0, []
    for e_path, item in (spec.get("paths") or {}).items():
        item = _resolve(spec, item)
        if not isinstance(item, dict):
            continue
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            total += 1
            sec = op.get("security", top)
            if sec:   # [] = 인증 없음, [{}] 도 사실상 없음
                if any(req for req in sec):
                    needed += 1
                    continue
            public.append(f"{method.upper()} {e_path}")
    return {"schemes": schemes, "needed": needed, "total": total, "public": public}


def security_lines(sec: dict[str, Any]) -> list[str]:
    """요약 첫머리에 넣을 '인증' 줄."""
    if not sec["schemes"] and not sec["needed"]:
        return ["인증: 문서에 인증 정의가 없음 (호출해서 401 이면 인증이 필요한 것 — 관리 화면에서 인증 방식을 정해야 함)"]
    out = []
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}   # 같은 방식·주소는 한 줄로 (예: 필수·선택 두 이름)
    for s in sec["schemes"]:
        key = (s["how"], s["doc_login_url"], s["header"])
        if key in merged:
            merged[key]["name"] += f"·{s['name']}"
        else:
            merged[key] = dict(s)
    for s in merged.values():
        if s["how"] == "login":
            line = f"인증: {s['name']} = OAuth2 password (아이디·비밀번호로 로그인 → 토큰). 문서의 로그인 주소(tokenUrl): {s['doc_login_url']}"
            if s["local"]:
                line += (f" — API 서버 자신(localhost)을 가리켜 바깥에서는 그대로 쓸 수 없음. 같은 호스트로 바꾸면 {s['login_url']}"
                         " (맞는지는 로그인해 봐야 안다)")
            out.append(line)
        elif s["how"] == "token":
            out.append(f"인증: {s['name']} = 토큰을 헤더 {s['header']} 에 넣음 ({s['type']})")
        elif s["how"] == "screen":
            out.append("인증: 저장된 화면에 자물쇠 표시가 있음 (인증 필요). 방식·로그인 주소는 저장 화면에 없어 알 수 없음 — "
                       "서버 명세를 읽을 수 없으면 사용자가 관리 화면에서 인증 방식(계정 로그인·토큰)을 정해야 함")
        else:
            out.append(f"인증: {s['name']} = {s['type']} ({s['how']} — 이 프로그램은 토큰·OAuth2 password 로그인만 지원)")
    pub = f" · 인증 없이 되는 것: {', '.join(sec['public'][:6])}" if sec["public"] else ""
    out.append(f"인증이 필요한 엔드포인트: {sec['needed']}/{sec['total']}개{pub}")
    return out


def auth_from_spec(spec: dict, spec_url: str, secret_base: str):
    """등록할 때 쓸 인증 설정 (Auth) — 문서에 로그인·토큰 방식이 정의돼 있고 실제로 쓰이면. 아니면 None."""
    from agent.apiauth import auth_for

    sec = security_info(spec, spec_url)
    if not sec["needed"]:
        return None
    for s in sec["schemes"]:
        if s["how"] == "login" and s["login_url"]:
            note = "문서의 OAuth2 password" + (f" (문서의 로그인 주소 {s['doc_login_url']} 가 localhost 라 같은 호스트로 바꿈)"
                                               if s["local"] else "")
            return auth_for("login", secret_base, login_url=s["login_url"], source=note)
    for s in sec["schemes"]:
        if s["how"] == "token":
            return auth_for("token", secret_base, header=s["header"], prefix="Bearer " if s["header"] == "Authorization" else "",
                            source=f"문서의 {s['type']} 토큰")
    return None


def describe(spec: dict, spec_url: str, local: bool = False) -> tuple[str, str]:
    """fetch_url 이 Swagger 문서를 읽었을 때 돌려줄 (제목, 요약). 첫 쪽에 인증·기능 그룹 전체가 오게 한다.
    local: 작업 폴더의 문서 파일에서 읽음 (API 기본 주소는 파일만으로 알 수 없다)."""
    info = spec.get("info") or {}
    title = f"API 문서: {info.get('title', '(제목 없음)')} {info.get('version', '')}".strip()
    eps = endpoints(spec)
    changing = sum(e["method"] != "GET" for e in eps)
    saved = " (브라우저로 저장한 Swagger 화면 — 메서드·경로·요약만 있고 파라미터·인증 상세는 없음)" if spec.get("x-source") else ""
    where = [f"문서 파일: {spec_url}{saved}", "API 기본 주소: 파일만으로는 알 수 없음 — 사용자가 알려 준 API 주소를 쓴다"] if local         else [f"명세 주소: {spec_url}", f"API 기본 주소: {base_url(spec, spec_url)}"]
    head = where + [
            f"엔드포인트 {len(eps)}개 (조회 {len(eps) - changing}, 변경 {changing})",
            f"기능 그룹(전체): {tag_overview(eps)}"]
    head += security_lines(security_info(spec, spec_url))
    if info.get("description"):
        head.append("설명: " + info["description"].strip().split("\n")[0][:200])
    body: list[str] = []
    for group in dict.fromkeys(e["tag"] for e in eps):
        body.append(f"[{group or '기타'}]")
        body += [endpoint_line(e) for e in eps if e["tag"] == group]
    tail = ["", "이 API 를 호출하려면 add_api(url='<API 주소>', docs_file='<이 파일>') 로 등록한다 (이 파일의 엔드포인트로 등록, 서버 문서는 읽지 않음)."
            if local else "이 API 를 호출하려면 add_api 로 등록한다 (이 문서 주소를 그대로 주면 엔드포인트·인증 방식을 자동으로 읽는다)."]
    return title, "\n".join(head + [""] + body + tail)
