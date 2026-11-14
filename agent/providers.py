"""외부 AI 모델 연결 (OpenAI · Claude(Anthropic) · Gemini(Google)).

관리자가 회사(우리 쪽) 계정을 연결해 두면, 로그인한 사람 누구나 입력창의 모델 선택에서 로컬 모델 대신 고를 수 있다.
- 연결 목록은 config/connections.yaml 의 providers, 키는 config/secrets.yaml(PROVIDER_<ID>_KEY)에만 둔다.
  키는 화면·채팅·모델 입력·실행 기록 어디에도 나가지 않는다 (화면에는 끝 4자리만).
- 연결 방식: API 키 (3사 모두), 또는 Claude 는 **회사 Claude 계정 로그인** — 관리자가 서버 PC 에서 공식 CLI 로
  `ant auth login --profile <이름>` 을 한 번 하면(브라우저에서 회사 Claude Console 계정으로 로그인) 그 프로필로 부른다.
  토큰은 %APPDATA%/Anthropic 에 공식 SDK 가 저장·갱신한다 (이 프로그램은 비밀번호도 토큰도 보지 않는다).
  OpenAI · Gemini 의 계정 로그인은 막혀 있다: 구독(ChatGPT·Gemini) 로그인을 다른 프로그램에서 쓰는 것은 OpenAI 는 승인받은
  앱만, Google 은 약관으로 금지 (README '외부 AI 모델').
- 폐쇄망 스위치(config/access.yaml offline)를 켜면 외부 연결을 하나도 시도하지 않는다.
"""

from __future__ import annotations

import re
import time
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from agent import config
from agent.config import Backend, get_secret

Kind = Literal["openai", "anthropic", "gemini"]
AuthMethod = Literal["api_key", "account_login"]

KINDS: dict[str, dict[str, Any]] = {
    "openai": {"title": "OpenAI", "base_url": "https://api.openai.com/v1", "context": 128_000},
    "anthropic": {"title": "Claude (Anthropic)", "base_url": "https://api.anthropic.com", "context": 200_000},
    "gemini": {"title": "Gemini (Google)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "context": 128_000},
}
ACCOUNT_LOGIN = {   # 공급처별 '계정 로그인' 가능 여부와 안내
    "anthropic": ("회사에서 받은 Claude Console(API) 계정으로 로그인합니다. 'Claude 계정으로 로그인'을 누르면 로그인 창이 열리고 "
                  "(서버 PC 에서 누르면 그 PC 에 브라우저가, 다른 PC 에서 누르면 여기 로그인 주소가 나옵니다), 회사 계정으로 로그인한 뒤 "
                  "조직·워크스페이스를 고르면 됩니다. 요금은 그 조직으로 청구됩니다. "
                  "(claude.ai 의 Pro·Max 구독 로그인은 다른 프로그램에서 쓸 수 없게 Anthropic 약관이 막고 있습니다.)"),
    "openai": ("OpenAI 계정 로그인은 쓸 수 없습니다. 'Sign in with ChatGPT'(ChatGPT 요금제로 쓰기)는 오픈소스 앱과 OpenAI 가 승인한 앱에만 "
               "열려 있고, 회사 서버에서 여러 사람이 쓰는 앱은 OpenAI 에 신청해 승인받아야 합니다. OpenAI Platform 에서 회사 API 키를 "
               "발급받아 넣으세요."),
    "gemini": ("Gemini 계정 로그인은 쓸 수 없습니다. Google 은 Gemini 앱·CLI 로그인을 다른 프로그램에서 쓰는 것을 약관으로 금지하고 "
               "계정을 정지합니다. Google AI Studio 에서 회사 API 키를 발급받아 넣으세요."),
}
ACCOUNT_LOGIN_NOTE = ACCOUNT_LOGIN["openai"]
ANT_EXE = "runtime/ant/ant.exe"   # Anthropic 공식 CLI (github.com/anthropics/anthropic-cli) — 계정 로그인에만 쓴다
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_ENABLED = 3          # 처음 연결할 때 켜 둘 최신 모델 수 (나머지는 관리자가 켠다)
# 대화용이 아닌 모델 (목록에서 뺀다)
_NOT_CHAT = re.compile(r"embed|tts|whisper|dall-e|audio|realtime|transcribe|moderation|image|search|veo|imagen|aqa|"
                       r"babbage|davinci|computer-use|sora|codex-mini", re.I)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class ProviderModel(BaseModel):
    id: str
    title: str = ""
    context_length: int = Field(default=128_000, ge=2048, le=10_000_000)
    enabled: bool = True


class Provider(BaseModel):
    id: str
    title: str = ""
    kind: Kind
    auth: AuthMethod = "api_key"
    base_url: str = ""               # 비우면 공급사 기본 주소 (회사 게이트웨이·프록시를 쓸 때만 바꾼다)
    profile: str = ""                # auth=account_login (Claude): ant auth login --profile 의 이름
    effort: str = ""                 # 추론 강도 (low · medium · high …). 비우면 공급사 기본
    enabled: bool = True
    models: list[ProviderModel] = Field(default_factory=list)
    checked_at: str = ""
    check_error: str = ""

    @property
    def secret_name(self) -> str:
        return f"PROVIDER_{self.id.upper().replace('-', '_')}_KEY"

    @property
    def url(self) -> str:
        return self.base_url or KINDS[self.kind]["base_url"]

    def key_hint(self) -> str:
        if self.auth == "account_login":
            return f"계정 로그인 (프로필 {self.profile})"
        key = get_secret(self.secret_name) or ""
        return f"…{key[-4:]}" if len(key) >= 8 else ("저장됨" if key else "")

    def has_credentials(self) -> bool:
        return self.auth == "account_login" and bool(self.profile) or bool(get_secret(self.secret_name))


# ───────────────────────── 저장 (connections.yaml 의 providers)

def _read() -> dict:
    path = config.CONNECTIONS_FILE
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def _write(data: dict) -> None:
    from agent import connections
    connections._write(data)


def all_providers() -> list[Provider]:
    return [Provider.model_validate(p) for p in _read().get("providers") or []]


def get(provider_id: str) -> Provider:
    found = next((p for p in all_providers() if p.id == provider_id), None)
    if found is None:
        raise KeyError(f"외부 모델 연결이 없습니다: {provider_id}")
    return found


def _save(provider: Provider) -> Provider:
    data = _read()
    items = [p for p in data.get("providers") or [] if p.get("id") != provider.id]
    old = next((i for i, p in enumerate(data.get("providers") or []) if p.get("id") == provider.id), len(items))
    items.insert(old, provider.model_dump(exclude_defaults=False))
    data["providers"] = items
    _write(data)
    return provider


def _new_id(kind: str, title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (title or kind).lower()).strip("-")[:24] or kind
    if not _ID_RE.match(base):
        base = kind
    taken = {p.id for p in all_providers()}
    pid, n = base, 2
    while pid in taken:
        pid, n = f"{base}-{n}", n + 1
    return pid


def add(kind: str, title: str, api_key: str, *, auth: str = "api_key", base_url: str = "", effort: str = "",
        profile: str = "", check: bool = True) -> Provider:
    from agent import connections
    if kind not in KINDS:
        raise ValueError("공급처는 openai · anthropic · gemini 중 하나입니다")
    if effort.strip() and effort.strip() not in EFFORTS:
        raise ValueError(f"추론 강도는 {', '.join(EFFORTS)} 중 하나입니다")
    if auth == "account_login":
        if kind != "anthropic":
            raise ValueError(ACCOUNT_LOGIN[kind])
        profile = profile.strip() or "agent"
        if not re.match(r"^[A-Za-z0-9_-]{1,40}$", profile):
            raise ValueError("프로필 이름은 영문·숫자·_- 만 (40자까지)")
    elif not api_key.strip():
        raise ValueError("API 키를 넣으세요")
    _check_url(base_url)
    provider = Provider(id=_new_id(kind, title), title=title.strip() or KINDS[kind]["title"], kind=kind,  # type: ignore[arg-type]
                        auth=auth, base_url=base_url.strip(), effort=effort.strip(),  # type: ignore[arg-type]
                        profile=profile if auth == "account_login" else "")
    if auth != "account_login":
        connections.set_secret(provider.secret_name, api_key.strip())
    _save(provider)
    return refresh(provider.id) if check else provider


def login_command(provider: Provider) -> str:
    """Claude 계정 로그인: 관리자가 서버 PC 에서 한 번 실행할 명령 (브라우저가 열린다)."""
    exe = config.ROOT / ANT_EXE
    return f'"{exe}" auth login --profile {provider.profile}'


def local_think(effort: str) -> bool | None:
    """입력창의 추론 강도 → 로컬 모델의 '생각' 스위치. 낮음 = 끄기(빠르게), 보통 이상 = 켜기, 기본 = 용도 설정대로."""
    return None if effort not in EFFORTS else effort != "low"


def effort_for(kind: str, effort: str) -> str:
    """입력창에서 고른 추론 강도를 공급사가 받는 값으로. OpenAI · Gemini 는 low · medium · high 까지."""
    if effort not in EFFORTS:
        return ""
    return effort if kind == "anthropic" else {"xhigh": "high", "max": "high"}.get(effort, effort)


def update(provider_id: str, *, title: str | None = None, api_key: str | None = None, base_url: str | None = None,
           effort: str | None = None, enabled: bool | None = None,
           models: list[dict[str, Any]] | None = None) -> Provider:
    from agent import connections
    provider = get(provider_id)
    if title is not None:
        provider.title = title.strip() or KINDS[provider.kind]["title"]
    if base_url is not None:
        _check_url(base_url)
        provider.base_url = base_url.strip()
    if effort is not None:
        if effort.strip() and effort.strip() not in EFFORTS:
            raise ValueError(f"추론 강도는 {', '.join(EFFORTS)} 중 하나입니다")
        provider.effort = effort.strip()
    if enabled is not None:
        provider.enabled = enabled
    if models is not None:   # 모델별 켜기·컨텍스트 길이 (목록에 있는 모델만)
        changes = {m["id"]: m for m in models}
        for m in provider.models:
            if m.id in changes:
                if "enabled" in changes[m.id]:
                    m.enabled = bool(changes[m.id]["enabled"])
                if changes[m.id].get("context_length"):
                    m.context_length = ProviderModel(id=m.id, context_length=int(changes[m.id]["context_length"])).context_length
    _save(provider)
    if api_key:
        connections.set_secret(provider.secret_name, api_key.strip())
        return refresh(provider.id)
    return provider


def remove(provider_id: str) -> None:
    from agent import connections
    provider = get(provider_id)
    data = _read()
    data["providers"] = [p for p in data.get("providers") or [] if p.get("id") != provider_id]
    _write(data)
    connections.remove_secrets([provider.secret_name])


def _check_url(url: str) -> None:
    url = url.strip()
    if url and not re.match(r"^https?://[^\s/]+", url):
        raise ValueError("주소는 http:// 또는 https:// 로 시작해야 합니다")


# ───────────────────────── 연결 확인 · 모델 목록

def anthropic_client(api_key: str = "", profile: str = "", base_url: str = "", timeout: float = 20, max_retries: int = 1):
    """공식 SDK 클라이언트. 계정 로그인이면 그 프로필(%APPDATA%/Anthropic — SDK 가 토큰을 갱신)로, 아니면 API 키로."""
    import anthropic
    url = base_url.rstrip("/") if base_url and base_url.rstrip("/") != KINDS["anthropic"]["base_url"] else None
    auth = {"profile": profile} if profile else {"api_key": api_key}
    return anthropic.Anthropic(**auth, base_url=url, timeout=timeout, max_retries=max_retries)


def _list_models(provider: Provider, key: str) -> list[ProviderModel]:
    """공급사에 실제로 물어 쓸 수 있는 모델 목록을 받는다 (최신 순)."""
    if provider.kind == "anthropic":
        client = anthropic_client(key, provider.profile if provider.auth == "account_login" else "", provider.base_url)
        out = []
        for m in client.models.list(limit=100):
            ctx = getattr(m, "max_input_tokens", None) or KINDS["anthropic"]["context"]
            out.append(ProviderModel(id=m.id, title=getattr(m, "display_name", "") or "", context_length=int(ctx)))
        return out
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=provider.url, timeout=20, max_retries=1)
    items = [m for m in client.models.list() if not _NOT_CHAT.search(m.id)]
    items.sort(key=lambda m: (getattr(m, "created", 0) or 0, m.id), reverse=True)
    out = []
    for m in items:
        mid = m.id.removeprefix("models/")
        title = (getattr(m, "model_extra", None) or {}).get("display_name") or ""
        out.append(ProviderModel(id=mid, title=str(title), context_length=KINDS[provider.kind]["context"]))
    return out


def refresh(provider_id: str) -> Provider:
    """연결 확인: 키로 모델 목록을 받아 본다. 성공하면 목록을 합치고(관리자가 바꾼 켜기·길이는 유지), 실패하면 이유를 남긴다."""
    from agent import accounts
    provider = get(provider_id)
    provider.checked_at = time.strftime("%Y-%m-%d %H:%M:%S")
    if accounts.load_access().offline:
        provider.check_error = "폐쇄망 스위치가 켜져 있어 외부 연결을 시도하지 않았습니다"
        return _save(provider)
    key = get_secret(provider.secret_name) or ""
    if not provider.has_credentials():
        provider.check_error = "API 키가 저장되지 않았습니다"
        return _save(provider)
    try:
        found = _list_models(provider, key)
    except Exception as e:   # noqa: BLE001 — 어떤 실패든 화면에 사유로 보여 준다
        provider.check_error = describe_error(e)
        return _save(provider)
    old = {m.id: m for m in provider.models}
    first = not old
    merged = []
    for i, m in enumerate(found):
        if m.id in old:
            m.enabled, m.context_length = old[m.id].enabled, old[m.id].context_length
        else:
            m.enabled = first and i < DEFAULT_ENABLED
        merged.append(m)
    provider.models = merged
    provider.check_error = "" if merged else "쓸 수 있는 대화 모델이 없습니다 (키의 권한을 확인하세요)"
    first = next((m for m in merged if m.enabled), None)
    if first is not None:   # 모델 목록은 잔액이 없어도 받아진다 → 아주 짧은 실제 호출로 잔액·권한까지 확인 (토큰 몇 개)
        try:
            _probe(provider, key, first.id)
        except Exception as e:   # noqa: BLE001
            if is_billing(e) or getattr(e, "status_code", None) in (401, 403):
                provider.check_error = describe_error(e)
    return _save(provider)


def _probe(provider: Provider, key: str, model_id: str) -> None:
    """'ping' 한 번 (출력 16토큰까지). 잔액 부족 · 권한 없음이면 예외. 요청 모양이 안 맞는 오류 등은 부른 쪽이 무시한다."""
    if provider.kind == "anthropic":
        client = anthropic_client(key, provider.profile if provider.auth == "account_login" else "", provider.base_url, 30, 0)
        client.messages.create(model=model_id, max_tokens=16, messages=[{"role": "user", "content": "ping"}])
        return
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=provider.url, timeout=30, max_retries=0)
    limit = {"max_completion_tokens": 16} if provider.kind == "openai" else {"max_tokens": 16}
    client.chat.completions.create(model=model_id, messages=[{"role": "user", "content": "ping"}], **limit)


BILLING_NOTE = ("결제 잔액·한도 부족 — 이 공급사 계정(조직)에 API 크레딧이 없거나 결제 한도를 넘었습니다. 관리자가 공급사 콘솔의 결제 화면에서 "
                "크레딧을 충전하거나 결제 수단을 등록해야 합니다 (Claude: Console › Plans & Billing · OpenAI: Platform › Billing · "
                "Gemini: AI Studio 결제 설정). Claude Pro/Max · ChatGPT Plus 같은 구독과 API 크레딧은 따로입니다.")
_BILLING = ("credit balance", "insufficient_quota", "exceeded your current quota", "billing", "payment required")


def is_billing(e: BaseException) -> bool:
    text = f"{getattr(e, 'message', '')} {e}".lower()
    return getattr(e, "status_code", None) == 402 or any(k in text for k in _BILLING)


def describe_error(e: BaseException) -> str:
    """공급사 오류를 사람이 할 일이 보이게 나눈다 (키 틀림 · 권한 · 한도 · 장애 · 인터넷 · 시간 초과)."""
    status = getattr(e, "status_code", None)
    name = type(e).__name__
    body = str(getattr(e, "message", "") or e)[:200]
    if is_billing(e):
        return BILLING_NOTE
    if name in ("CredentialsError", "WorkloadIdentityError", "IdentityTokenFileError"):
        return "아직 Claude 계정으로 로그인하지 않았거나 로그인이 만료됐습니다 — 관리자가 '관리 › 외부 AI 모델'에서 'Claude 계정으로 로그인'을 누르세요."
    if "Timeout" in name:
        return "시간 초과 — 공급사가 제때 답하지 않았습니다. 잠시 뒤 다시 시도하세요."
    if "Connection" in name:
        return "인터넷 연결 안 됨 — 이 서버에서 공급사 주소로 나갈 수 없습니다 (방화벽·프록시·폐쇄망 확인)."
    if status == 401:
        return "키가 틀렸거나 만료됐습니다 (HTTP 401) — 관리자가 '외부 모델 연결'에서 키를 다시 넣어야 합니다."
    if status == 403:
        return f"권한 없음 (HTTP 403) — 이 키로 쓸 수 없는 기능·지역이거나 계정이 막혔습니다. {body}"
    if status == 404:
        return f"모델에 접근할 수 없습니다 (HTTP 404) — 모델 이름이 바뀌었거나 이 키로 쓸 수 없는 모델입니다. {body}"
    if status == 429:
        return "한도·요금 초과 (HTTP 429) — 사용량 한도나 결제 잔액을 확인하세요. 잠시 뒤 다시 시도할 수 있습니다."
    if status in (500, 502, 503, 504, 529):
        return f"공급사 장애 (HTTP {status}) — 공급사 쪽 문제입니다. 잠시 뒤 다시 시도하세요."
    if status:
        return f"요청 실패 (HTTP {status}): {body}"
    return f"{name}: {body}"


# ───────────────────────── 실행에 쓰기

def choice_key(provider_id: str, model_id: str) -> str:
    return f"ext:{provider_id}:{model_id}"


def is_external(choice: str) -> bool:
    return choice.startswith("ext:")


def resolve(choice: str) -> Backend:
    """입력창에서 고른 외부 모델(ext:<연결>:<모델>)을 실행에 쓸 Backend 로. 쓸 수 없으면 ValueError (사유 포함)."""
    from agent import accounts
    if accounts.load_access().offline:
        raise ValueError("폐쇄망 스위치가 켜져 있어 외부 모델을 쓸 수 없습니다. 로컬 모델을 고르세요.")
    try:
        _, pid, model_id = choice.split(":", 2)
        provider = get(pid)
    except (ValueError, KeyError):
        raise ValueError(f"고른 외부 모델을 찾을 수 없습니다 ({choice}). 모델 선택을 다시 고르세요.")
    model = next((m for m in provider.models if m.id == model_id), None)
    if not provider.enabled or model is None or not model.enabled:
        raise ValueError(f"'{provider.title} · {model_id}' 는 관리자가 꺼 두었습니다. 다른 모델을 고르세요.")
    if not provider.has_credentials():
        raise ValueError(f"'{provider.title}' 의 API 키가 없습니다. 관리자에게 알리세요.")
    login = provider.auth == "account_login"
    return Backend(provider=provider.kind, base_url=provider.url, api_key="" if login else get_secret(provider.secret_name) or "",
                   profile=provider.profile if login else "", model=model.id, context_length=model.context_length,
                   timeout=600, effort=provider.effort, connection=provider.id)


def choices() -> list[dict[str, Any]]:
    """입력창 모델 선택에 보일 외부 모델 (켜 둔 연결의 켜 둔 모델). 키 값은 넣지 않는다."""
    from agent import accounts
    if accounts.load_access().offline:
        return []
    out = []
    for p in all_providers():
        if not p.enabled:
            continue
        ready = p.has_credentials() and not p.check_error
        for m in p.models:
            if m.enabled:
                out.append({"key": choice_key(p.id, m.id), "connection": p.id, "connection_title": p.title, "kind": p.kind,
                            "model": m.id, "title": m.title or m.id, "context_length": m.context_length,
                            "ready": ready, "problem": p.check_error})
    return out
