"""사내 API 인증 헤더 만들기 — 고정 토큰, 또는 계정으로 로그인해 받은 토큰 (QA API-6).

로그인은 OAuth2 password 방식(대부분의 사내 API·FastAPI 기본): login_url 에 form-urlencoded 로
grant_type=password · username · password 를 보내고, 응답 JSON 의 access_token(·token_type·expires_in)을 쓴다.

- 계정은 관리 화면에서만 넣는다 (config/secrets.yaml). 모델·채팅·실행 기록에는 계정도 토큰도 나가지 않는다.
- 로그인 사용 모드(여러 사람)에서는 로그인으로 받는 API 를 **각자 자기 계정**으로 부른다 (화면 '내 계정' ›
  사내 API 계정 → config/user_secrets.yaml). 본인 계정이 없으면 다른 사람·공용 계정으로 대신 부르지 않는다.
- 받은 토큰은 이 프로세스 메모리에만 둔다. 만료 시각이 지나거나 API 가 401 을 주면 다시 로그인한다.
- 오류 메시지에도 값은 넣지 않는다 (서버 응답에 섞여 와도 secret_values 로 가린다).
"""

from __future__ import annotations

import base64
import re
import json
import threading
import time
from typing import Any

import httpx

from agent import accounts
from agent.config import ApiEndpoint, Auth, get_secret
from agent.tools.base import ToolError

UI_PLACE = "관리 화면 '상태 · 연결' › 사내 API › '{name}' 의 인증 칸"
UI_PLACE_PERSONAL = "관리 화면 '내 계정' › 사내 API 계정 › '{name}'"
_TOKEN_KEYS = ("access_token", "accessToken", "token", "jwt", "id_token")
_DEFAULT_TTL = 30 * 60
_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}   # API 이름 → (헤더 값, 만료 시각)


def _personal(api: ApiEndpoint) -> str | None:
    """이 API 를 사람마다 자기 계정으로 부르는지: 로그인으로 받는 API + 로그인 사용 모드면 지금 사용자 id."""
    return accounts.personal_secrets() if api.auth is not None and api.auth.kind == "login" else None


def ui_place(api: ApiEndpoint) -> str:
    return (UI_PLACE_PERSONAL if _personal(api) else UI_PLACE).format(name=api.name)


def secret(api: ApiEndpoint, name: str) -> str | None:
    """인증 값 하나. 사람마다 쓰는 계정이면 그 사람의 값만 본다 (없으면 None — 공용 값으로 대신하지 않는다)."""
    if not name:
        return None
    uid = _personal(api)
    return accounts.get_user_secret(uid, name) if uid else get_secret(name)


def missing(api: ApiEndpoint) -> list[str]:
    a = api.auth
    if a is None:
        return []
    names = [a.username_secret, a.password_secret] if a.kind == "login" else [a.secret]
    return [n for n in names if not n or not secret(api, n)]


def _key(api: ApiEndpoint) -> str:
    a = api.auth
    who = _personal(api) or ""
    return f"{api.name}|{a.login_url}|{a.username_secret}|{who}" if a else api.name


def forget(api: ApiEndpoint) -> None:
    with _lock:
        _cache.pop(_key(api), None)


def _jwt_exp(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return float(payload["exp"]) if "exp" in payload else None
    except (ValueError, KeyError, TypeError):
        return None


def missing_message(api: ApiEndpoint) -> str:
    """계정·토큰이 아직 없을 때 모델에게 줄 말 (사용자가 할 일 · 채팅 금지)."""
    a = api.auth
    if a is not None and a.kind == "token":
        return (f"인증 값 '{a.secret}' 이 설정되지 않았습니다. 사용자가 {ui_place(api)} 에 토큰을 넣어야 합니다 "
                "(또는 환경변수·config/secrets.yaml). 채팅으로 받지 마세요.")
    if _personal(api):
        return (f"'{api.name}' 은 각자 자기 계정으로 로그인해야 하는 API 입니다. 이 사용자의 계정이 아직 없습니다 — "
                f"사용자가 {ui_place(api)} 에 본인 아이디·비밀번호를 넣어야 합니다. 채팅으로 받지 말고, 넣은 뒤 다시 요청하라고 "
                "안내하세요. 다른 사람의 계정으로 대신 조회하지 않습니다.")
    return (f"'{api.name}' 은 로그인이 필요한 API 입니다 ({a.source if a and a.source else '로그인'}). 아직 계정이 저장되지 않았습니다 — "
            f"사용자가 {ui_place(api)} 에 아이디·비밀번호를 넣어야 합니다. 채팅으로 받지 말고, 넣은 뒤 다시 요청하라고 "
            "안내하세요. 넣기 전에는 이 경로를 조회할 수 없습니다.")


def login(api: ApiEndpoint, check_url: Any = None) -> tuple[str, float]:
    """로그인해서 (헤더 값, 만료 시각). check_url: 주소 허용 검사 함수 (settings 를 묶어 넘긴다)."""
    a = api.auth
    assert a is not None and a.kind == "login"
    if not a.login_url:
        raise ToolError(f"'{api.name}' 은 로그인이 필요한 API 인데 로그인 주소가 없습니다. 사용자가 {ui_place(api)} 에서 "
                        "로그인 주소를 넣어야 합니다.")
    if missing(api):
        raise ToolError(missing_message(api))
    if check_url is not None:
        check_url(a.login_url)
    form = {"grant_type": "password", "username": secret(api, a.username_secret) or "",
            "password": secret(api, a.password_secret) or ""}
    try:
        with httpx.Client(timeout=20, follow_redirects=False, verify=api.verify_tls) as c:
            r = c.post(a.login_url, data=form, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise ToolError(f"'{api.name}' 로그인 주소({a.login_url})에 접속하지 못했습니다 ({type(e).__name__}). "
                        f"주소가 맞는지 사용자가 {ui_place(api)} 에서 확인해야 합니다.")
    if r.status_code in (400, 401, 403):
        raise ToolError(f"'{api.name}' 로그인이 거절됐습니다 (HTTP {r.status_code}). 저장된 아이디·비밀번호가 맞지 않거나 권한이 "
                        f"없습니다 — 사용자가 {ui_place(api)} 에서 계정을 확인해야 합니다.")
    if r.status_code >= 300:
        raise ToolError(f"'{api.name}' 로그인 주소가 HTTP {r.status_code} 를 돌려줬습니다. 로그인 주소({a.login_url})가 맞는지 "
                        f"사용자가 {ui_place(api)} 에서 확인해야 합니다.")
    try:
        data = r.json()
    except ValueError:
        data = {}
    token = next((data[k] for k in _TOKEN_KEYS if isinstance(data, dict) and isinstance(data.get(k), str) and data[k]), "")
    if not token:
        raise ToolError(f"'{api.name}' 로그인 응답에 토큰(access_token)이 없습니다. 로그인 방식이 OAuth2 password 가 아닐 수 "
                        "있습니다 — 사용자가 관리 화면에서 토큰을 직접 넣는 방식으로 바꿔야 합니다.")
    kind = str(data.get("token_type") or "Bearer")
    value = (a.prefix or f"{kind[:1].upper()}{kind[1:].lower()} ") + token
    ttl = data.get("expires_in")
    expires = time.time() + float(ttl) if isinstance(ttl, (int, float)) else (_jwt_exp(token) or time.time() + _DEFAULT_TTL)
    return value, expires - 30   # 만료 30초 전부터는 새로 받는다


def headers(api: ApiEndpoint, check_url: Any = None, fresh: bool = False) -> dict[str, str]:
    """요청에 넣을 인증 헤더. 값이 없으면 사용자가 무엇을 해야 하는지 담은 ToolError."""
    a = api.auth
    if a is None:
        return {}
    if a.kind == "token":
        value = get_secret(a.secret) if a.secret else None
        if not value:
            raise ToolError(missing_message(api))
        return {a.header: a.prefix + value}
    key = _key(api)
    with _lock:
        cached = None if fresh else _cache.get(key)
    if cached is None or cached[1] < time.time():
        cached = login(api, check_url)
        with _lock:
            _cache[key] = cached
    return {a.header: cached[0]}


def secret_values(api: ApiEndpoint, sent: dict[str, str]) -> set[str]:
    """응답·오류에서 가릴 값: 보낸 헤더 값, 접두어 뺀 토큰, 아이디·비밀번호."""
    out = set(sent.values())
    for v in sent.values():
        if " " in v:
            out.add(v.split(" ", 1)[1])
    a = api.auth
    if a is not None:
        out |= {v.removeprefix(a.prefix) for v in sent.values() if a.prefix}
        for name in (a.secret, a.username_secret, a.password_secret):
            if name and (value := secret(api, name)):
                out.add(value)
    return {s for s in out if len(s) >= 4}


def describe(api: ApiEndpoint) -> str:
    """시스템 프롬프트·도구 결과에 쓸 인증 상태 한 줄 (값은 없음)."""
    a = api.auth
    if a is None:
        return "인증 설정 없음"
    gap = missing(api)
    state = "계정 저장됨" if a.kind == "login" and not gap else "토큰 저장됨" if not gap else \
        f"아직 {'계정' if a.kind == 'login' else '토큰'} 없음 — 사용자가 {ui_place(api)} 에 넣어야 함"
    if a.kind == "login" and _personal(api):
        state += " (각자 자기 계정으로 조회)"
    how = f"로그인(OAuth2 password) · 로그인 주소 {a.login_url}" if a.kind == "login" else f"토큰 헤더 {a.header}"
    return f"{how} · {state}" + (f" · {a.source}" if a.source else "")


def check_login(api: ApiEndpoint, check_url: Any = None) -> str:
    """관리 화면의 '로그인 확인' — 토큰은 돌려주지 않는다."""
    forget(api)
    headers(api, check_url, fresh=True)
    return "로그인 성공 (토큰을 받았습니다. 토큰은 저장하지 않고 이 프로그램 메모리에만 둡니다)"


# 채팅에 사용자가 직접 적은 자격 증명 ('비밀번호는 abc123', 'token: eyJ…') — 모델·기록에 넘기기 전에 가린다 (QA API-4)
_TYPED = re.compile(r"(?P<key>비밀\s*번호|비번|패스워드|암호|password|passwd|pwd|토큰|\btoken\b|access[\s_-]?token|api[\s_-]?key|"
                    r"secret|아이디|username|user\s*name|로그인\s*id)\s*(?:[:=]|(?:은|는|이|가|을|를)\s)\s*[\"'`]?(?P<val>[^\s\"'`,;]{3,})",
                    re.I)
MASK_NOTE = ("(이 메시지에 적힌 비밀번호·토큰 같은 값은 보안상 가리고 저장하지 않았습니다. 사용자에게 관리 화면 '상태 · 연결' › "
             "사내 API › 그 API 의 인증 칸에 넣어 달라고 안내하세요.)")


def mask_credentials(text: str) -> tuple[str, int]:
    """(가린 글자, 가린 개수). 한글이 섞인 값('3개', '관리 화면에')은 값이 아니라 문장이라 두고, 숫자만 있는 짧은 값도 둔다."""
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        val = m.group("val")
        if re.search(r"[가-힣]", val) or (val.isdigit() and len(val) < 6):
            return m.group(0)
        count += 1
        return m.group(0)[: m.start("val") - m.start()] + "***"

    return _TYPED.sub(repl, text), count


def auth_for(kind: str, secret_base: str, **kw: Any) -> Auth:
    """등록할 때 쓰는 기본 인증 설정. 키 이름은 API 마다 따로 (BASE_TOKEN / BASE_USERNAME / BASE_PASSWORD)."""
    if kind == "login":
        return Auth(kind="login", username_secret=f"{secret_base}_USERNAME", password_secret=f"{secret_base}_PASSWORD", **kw)
    return Auth(kind="token", secret=f"{secret_base}_TOKEN", **kw)
