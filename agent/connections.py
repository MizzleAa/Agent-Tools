"""화면·채팅으로 사내 사이트와 API 를 등록/삭제한다. 저장 위치는 config/connections.yaml.

settings.yaml 은 사람이 주석을 달아 관리하는 파일이라 프로그램이 다시 쓰면 주석이 사라진다.
그래서 자동으로 추가되는 항목은 별도 파일에 두고, load_settings() 가 두 파일을 합친다.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import yaml

from agent import config
from agent.config import ApiEndpoint, Auth, IntranetSource, Settings

# 수집할 때 절대 따라가면 안 되는 링크 (로그아웃·편집·삭제). 사용자가 나중에 connections.yaml 에서 고칠 수 있다.
DEFAULT_EXCLUDES = ["logout", "logoff", "signout", "action=edit", "action=delete", "/delete", "/edit"]
_HEADER = "# 관리 화면·에이전트가 저장하는 연결 목록 (사내 사이트, 사내 API, 고른 모델·추가한 모델). 직접 고쳐도 된다.\n"
_SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _read() -> dict:
    path = config.CONNECTIONS_FILE
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def text_of(data: dict) -> str:
    return _HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _write(data: dict) -> None:
    config.CONNECTIONS_FILE.write_text(text_of(data), encoding="utf-8")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "site"


def api_name(text: str) -> str:
    """도구에 쓰는 API 이름: 소문자 영문·숫자·한글과 '-'. 한글 이름도 살린다 (QA API-7: '회사 …' 가 'site' 로 바뀜)."""
    return re.sub(r"[\W_]+", "-", text.strip().lower()).strip("-")[:30]


def secret_base(name: str, base_url: str) -> str:
    """토큰·계정을 저장할 키 이름의 앞부분 (영문 대문자). 한글 이름이면 주소(호스트·포트)로 만든다."""
    ascii_name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    if ascii_name and re.fullmatch(r"[\x00-\x7f]*", name):
        return ascii_name[:40]
    host = re.sub(r"[^A-Za-z0-9]+", "_", urlsplit(base_url).netloc).strip("_").upper()
    return f"API_{host}"[:40]


def _unique(base: str, taken: set[str]) -> str:
    name, i = base, 2
    while name in taken:
        name, i = f"{base}-{i}", i + 1
    return name


def add_source(settings: Settings, url: str, name: str = "", description: str = "",
               whole_site: bool = False) -> IntranetSource:
    """사이트를 등록하고 settings 객체에도 바로 반영한다 (같은 실행 안에서 곧바로 쓸 수 있게)."""
    url = url.strip().split("#")[0]
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"http:// 또는 https:// 로 시작하는 주소를 주세요: {url}")
    if not p.path:
        url, p = url + "/", urlsplit(url + "/")
    for s in settings.intranet.sources:
        if any(url.startswith(prefix) for prefix in s.prefixes()):
            raise ValueError(f"이미 등록된 사이트 '{s.name}' 의 수집 범위 안에 있는 주소입니다")
    # 수집 범위: 사이트 전체, 또는 준 주소가 있는 폴더 아래 (예: /display/PROC/Home → /display/PROC/ 아래)
    folder = p.path.rsplit("/", 1)[0] + "/"
    prefix = f"{p.scheme}://{p.netloc}{'/' if whole_site else folder}"
    first_dir = p.path.strip("/").split("/")[0] if not whole_site and folder != "/" else ""
    base = _slug(name) if name else _slug(p.hostname.split(".")[0] + (f"-{first_dir}" if first_dir else ""))
    src = IntranetSource(
        name=_unique(base, {s.name for s in settings.intranet.sources}),
        description=description or f"{p.hostname} 사내 페이지",
        start_urls=[url], include_prefixes=[prefix], exclude_patterns=list(DEFAULT_EXCLUDES))
    data = _read()
    data.setdefault("intranet_sources", []).append(src.model_dump(exclude_none=True))
    _write(data)
    src.managed = True
    settings.intranet.sources.append(src)
    return src


def remove_source(name: str) -> None:
    data = _read()
    items = data.get("intranet_sources") or []
    if not any(item.get("name") == name for item in items):
        raise ValueError(f"'{name}' 은 화면·채팅으로 등록한 사이트가 아닙니다 (settings.yaml 에 있다면 거기서 지우세요)")
    data["intranet_sources"] = [item for item in items if item.get("name") != name]
    _write(data)


def add_api(settings: Settings, name: str, base_url: str, description: str = "", docs: str = "",
            auth_secret: str = "", auth_header: str = "Authorization", auth_prefix: str = "",
            docs_url: str = "", auth: Auth | None = None) -> ApiEndpoint:
    """API 를 조회(GET) 전용으로 등록한다. 변경 요청(POST 등)은 관리 화면의 '변경 요청 허용' 으로 켠다.
    auth: 문서에서 읽은 인증 설정 (auth_secret 보다 우선)."""
    p = urlsplit(base_url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"http:// 또는 https:// 로 시작하는 주소를 주세요: {base_url}")
    title = name.strip()
    name = api_name(title) or api_name(p.hostname.split(".")[0]) or "api"
    if name in {a.name for a in settings.apis}:
        raise ValueError(f"같은 이름의 API 가 이미 있습니다: {name}")
    if auth_secret and not _SECRET_NAME.match(auth_secret):
        raise ValueError("인증 값 이름은 영문·숫자·_ 만 쓸 수 있습니다 (예: MES_TOKEN)")
    if auth is None and auth_secret:
        auth = Auth(header=auth_header, secret=auth_secret, prefix=auth_prefix)
    elif auth is not None and not auth_secret:
        auth = reuse_account(auth, base_url)   # API-16: 같은 서버에 저장된 계정
    api = ApiEndpoint(name=name, title=title if title and title != name else "", description=description,
                      base_url=base_url.strip().rstrip("/"), docs=docs, docs_url=docs_url, auth=auth)
    data = _read()
    data.setdefault("apis", []).append(api.model_dump(exclude_none=True))
    _write(data)
    api.managed = True
    settings.apis.append(api)
    return api


def read_openapi(settings: Settings, url: str) -> tuple[str, dict]:
    """등록 전이라 아직 허용되지 않은 내부 주소라도, 등록하려는 그 호스트만 잠깐 허용해 명세를 읽는다."""
    from agent import openapi

    host = (urlsplit(url).hostname or "").lower()
    _check_rule(host)
    temp = settings.model_copy(deep=True)
    temp.web.allow_private_hosts.append(host)
    try:
        return openapi.load_spec(temp, url)
    except Exception as e:   # ToolError 등 → 호출한 쪽이 사용자에게 보여줄 메시지로
        raise ValueError(str(e))


def add_api_from_docs(settings: Settings, url: str, name: str = "", description: str = "",
                      auth_secret: str = "", auth_prefix: str = "Bearer ") -> tuple[ApiEndpoint, dict]:
    """Swagger/OpenAPI 문서 주소 하나로 API 를 등록한다: 기본 주소·이름·설명·엔드포인트 목록을 명세에서 뽑는다."""
    from agent import openapi

    spec_url, spec = read_openapi(settings, url)
    info = spec.get("info") or {}
    title = name or info.get("title", "")
    base = openapi.base_url(spec, spec_url)
    auth = None if auth_secret else openapi.auth_from_spec(spec, spec_url, secret_base(title or urlsplit(base).hostname or "", base))
    api = add_api(settings, title, base,
                  description or (info.get("title", "") + (f" — {info['description'].strip().splitlines()[0][:120]}"
                                                            if info.get("description") else "")),
                  openapi.docs_text(spec), auth_secret, auth_prefix=auth_prefix if auth_secret else "",
                  docs_url=url, auth=auth)
    return api, spec


def _api_item(data: dict, name: str) -> dict:
    item = next((x for x in data.get("apis") or [] if x.get("name") == name), None)
    if item is None:
        raise ValueError(f"'{name}' 은 화면·채팅으로 등록한 API 가 아닙니다 (settings.yaml 에 있다면 거기서 고치세요)")
    return item


def set_api_auth(settings: Settings, name: str, kind: str, login_url: str = "", header: str = "Authorization",
                 prefix: str = "") -> Auth | None:
    """관리 화면에서 인증 방식을 정한다 (none · token · login). 값(토큰·계정)은 set_api_credentials 로 따로."""
    from agent import apiauth

    data = _read()
    item = _api_item(data, name)
    api = next((a for a in settings.apis if a.name == name), None)
    base = secret_base(item.get("title") or name, item.get("base_url", ""))
    if kind == "none":
        auth = None
    elif kind == "login":
        p = urlsplit(login_url.strip())
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ValueError(f"로그인 주소는 http:// 또는 https:// 로 시작해야 합니다: {login_url}")
        _check_rule((p.hostname or "").lower())
        old = (item.get("auth") or {}).get("source", "") if (item.get("auth") or {}).get("kind") == "login" else ""
        auth = apiauth.auth_for("login", base, login_url=login_url.strip(), source=old or "관리 화면에서 설정")
    elif kind == "token":
        if not re.fullmatch(r"[A-Za-z0-9\-]{1,60}", header):
            raise ValueError(f"헤더 이름이 올바르지 않습니다: {header}")
        auth = apiauth.auth_for("token", base, header=header, prefix=prefix, source="관리 화면에서 설정")
    else:
        raise ValueError(f"인증 방식은 none · token · login 중 하나입니다: {kind}")
    auth = reuse_account(auth, item.get("base_url", ""), data)
    if auth is None:
        item.pop("auth", None)
    else:
        item["auth"] = auth.model_dump(exclude_defaults=True)
    _write(data)
    if api is not None:
        api.auth = auth
        apiauth.forget(api)
    return auth


def set_api_credentials(settings: Settings, name: str, token: str = "", username: str = "", password: str = "") -> None:
    """토큰 또는 계정을 config/secrets.yaml 에 저장한다 (관리 화면에서만). 비운 칸은 그대로 둔다."""
    from agent import apiauth

    api = next((a for a in settings.apis if a.name == name), None)
    if api is None or api.auth is None:
        raise ValueError(f"'{name}' 에 인증 방식이 정해지지 않았습니다. 먼저 인증 방식을 고르세요")
    a = api.auth
    values = {a.secret: token} if a.kind == "token" else {a.username_secret: username, a.password_secret: password}
    for key, value in values.items():
        if key and value:
            set_secret(key, value)
    base = (a.username_secret or a.secret).rsplit("_", 1)[0]
    if base and any(values.values()):   # 이 서버의 계정이 어느 키에 있는지 남긴다 (다른 이름으로 다시 등록해도 찾게)
        data = _read()
        data.setdefault("account_keys", {})[host_key(api.base_url)] = base
        _write(data)
    apiauth.forget(api)


def refresh_api_docs(settings: Settings, name: str) -> int:
    """API 가 바뀌었을 때 등록할 때 쓴 문서 주소에서 엔드포인트 목록을 다시 읽는다. 엔드포인트 수를 돌려준다."""
    from agent import openapi

    data = _read()
    item = next((x for x in data.get("apis") or [] if x.get("name") == name), None)
    if item is None or not item.get("docs_url"):
        raise ValueError(f"'{name}' 은 문서 주소로 등록한 API 가 아닙니다")
    _, spec = read_openapi(settings, item["docs_url"])
    item["docs"] = openapi.docs_text(spec)
    _write(data)
    return len(openapi.endpoints(spec))


def set_api_write(name: str, allow: bool) -> list[str]:
    """'변경 요청 허용' 스위치. 켜면 POST/PUT/PATCH/DELETE 도 api_send 로 부를 수 있다 (매번 승인은 그대로)."""
    data = _read()
    item = next((x for x in data.get("apis") or [] if x.get("name") == name), None)
    if item is None:
        raise ValueError(f"'{name}' 은 화면·채팅으로 등록한 API 가 아닙니다 (settings.yaml 에 있다면 거기서 고치세요)")
    item["allow_methods"] = ["GET", "POST", "PUT", "PATCH", "DELETE"] if allow else ["GET"]
    _write(data)
    return item["allow_methods"]


def _check_rule(rule: str) -> None:
    """허용하면 위험한 범위를 막는다: 로컬(모델·관리 서버가 있는 곳), 너무 넓은 대역."""
    import ipaddress

    if not rule or rule in ("localhost", "*") or rule.endswith(".localhost"):
        raise ValueError("로컬 주소는 허용할 수 없습니다 (모델 서버·관리 화면 보호)")
    try:
        net = ipaddress.ip_network(rule, strict=False)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*", rule):
            raise ValueError(f"호스트 이름·IP·대역(CIDR) 형식이 아닙니다: {rule}")
        return
    if net.is_loopback or net.is_unspecified or net.is_link_local:
        raise ValueError("로컬·링크로컬 주소는 허용할 수 없습니다 (모델 서버·관리 화면 보호)")
    if net.prefixlen < (8 if net.version == 4 else 32):
        raise ValueError(f"범위가 너무 넓습니다: {rule} (IPv4 는 /8 이상으로 좁혀 주세요)")


def allow_host(settings: Settings, host: str) -> str:
    """내부망 주소(호스트·IP·CIDR, 또는 URL 을 주면 그 호스트)를 읽기 허용 목록에 저장한다."""
    raw = host.strip()
    if "://" in raw:                                   # http://10.1.2.3:20000/docs → 10.1.2.3
        rule = urlsplit(raw).hostname or ""
    elif re.fullmatch(r"[\d.]+/\d{1,2}", raw):         # 10.20.0.0/16 (대역)
        rule = raw
    else:                                              # 10.1.2.3:20000/docs, wiki.intra → 호스트만
        rule = raw.split("/")[0].rsplit(":", 1)[0] if raw.count(":") == 1 else raw.split("/")[0]
    rule = rule.lower().strip("[]")
    _check_rule(rule)
    data = _read()
    rules = data.setdefault("allow_private_hosts", [])
    if rule not in rules:
        rules.append(rule)
        _write(data)
    if rule not in settings.web.allow_private_hosts:
        settings.web.allow_private_hosts.append(rule)
    return rule


def remove_host(rule: str) -> None:
    data = _read()
    rules = data.get("allow_private_hosts") or []
    if rule not in rules:
        raise ValueError(f"'{rule}' 은 화면·채팅으로 허용한 주소가 아닙니다 (settings.yaml 에 있다면 거기서 지우세요)")
    data["allow_private_hosts"] = [r for r in rules if r != rule]
    _write(data)


def _secret_names(auth: dict | None) -> set[str]:
    a = auth or {}
    return {a.get(k) for k in ("secret", "username_secret", "password_secret") if a.get(k)}


def remove_api(name: str) -> list[str]:
    """등록을 지운다. 그 등록에만 쓰인 계정·토큰도 secrets.yaml 에서 지운다 (QA API-17). 지운 키 이름을 돌려준다."""
    data = _read()
    items = data.get("apis") or []
    item = next((x for x in items if x.get("name") == name), None)
    if item is None:
        raise ValueError(f"'{name}' 은 화면·채팅으로 등록한 API 가 아닙니다 (settings.yaml 에 있다면 거기서 지우세요)")
    data["apis"] = [x for x in items if x.get("name") != name]
    still = set().union(*(_secret_names(x.get("auth")) for x in data["apis"] + (data.get("intranet_sources") or [])))
    gone = sorted(_secret_names(item.get("auth")) - still)
    keys = data.get("account_keys") or {}
    for host, base in list(keys.items()):   # 이 서버 계정 위치 기록도, 다른 등록이 안 쓰면 지운다
        if any(n.startswith(base + "_") for n in gone) and not any(n.startswith(base + "_") for n in still):
            keys.pop(host)
    _write(data)
    remove_secrets(gone)
    return gone


def remove_secrets(names: list[str]) -> None:
    path = config.SECRETS_FILE
    if not names or not path.exists():
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not any(n in data for n in names):
        return
    for n in names:
        data.pop(n, None)
    path.write_text("# 토큰·비밀번호. git·오프라인 번들에 포함되지 않는다.\n"
                    + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def host_key(url: str) -> str:
    return urlsplit(url).netloc.lower()


def reuse_account(auth: Auth | None, base_url: str, data: dict | None = None) -> Auth | None:
    """같은 서버(호스트:포트)에 이미 저장된 계정·토큰이 있으면 그 키 이름을 쓴다 (QA API-16: 이름을 바꿔 다시 등록하면
    '계정 없음' 이 됐다). 관리 화면에서 저장할 때 남긴 기록 → 주소로 만든 기본 이름 순서로 찾는다."""
    if auth is None or not auth.missing():
        return auth
    from agent import apiauth
    known = ((data if data is not None else _read()).get("account_keys") or {}).get(host_key(base_url))
    for base in [b for b in (known, secret_base("", base_url)) if b]:
        cand = apiauth.auth_for(auth.kind, base, **{k: v for k, v in auth.model_dump().items()
                                                   if k in ("header", "prefix", "login_url", "source")})
        if not cand.missing():
            cand.source = (cand.source + " · " if cand.source else "") + "같은 서버에 저장돼 있던 계정을 씀"
            return cand
    return auth


def set_active_model(settings: Settings, name: str) -> None:
    """관리 화면의 모델 선택을 저장한다. 파일이 없는 모델은 고를 수 없다."""
    preset = next((m for m in settings.models if m.name == name), None)
    if preset is None:
        raise ValueError(f"모델 목록에 없는 이름입니다: {name}")
    state = config.model_file_state(settings.path(preset.file))
    if state != "ready":
        raise ValueError(f"모델 파일이 {'아직 내려받는 중' if state == 'downloading' else '없습니다'}: {settings.path(preset.file)}")
    data = _read()
    data["active_model"] = name
    _write(data)


def set_runtime_port(port: int) -> None:
    """모델 서버 포트를 다른 프로그램이 쓰고 있어 바꾼 포트를 기억한다 (에이전트·eval·CLI 가 같은 포트를 쓰게)."""
    data = _read()
    data["runtime_port"] = int(port)
    _write(data)


def set_secret(name: str, value: str) -> None:
    """토큰을 config/secrets.yaml 에 저장한다. 관리 화면에서만 부른다 (모델은 이 값을 볼 수 없어야 함)."""
    if not _SECRET_NAME.match(name):
        raise ValueError("인증 값 이름은 영문·숫자·_ 만 쓸 수 있습니다")
    path = config.SECRETS_FILE
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    data[name] = value
    path.write_text("# 토큰·비밀번호. git·오프라인 번들에 포함되지 않는다.\n"
                    + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
