"""settings.yaml 을 읽어 타입이 있는 Settings 객체로 만든다."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
SECRETS_FILE = CONFIG_DIR / "secrets.yaml"
CONNECTIONS_FILE = CONFIG_DIR / "connections.yaml"   # 화면·채팅으로 등록한 사이트·API (기계가 쓰는 파일)


def model_file_state(path: Path) -> str:
    """'ready' | 'downloading'(최근 30초 안에 크기가 바뀜) | 'missing'"""
    if not path.exists():
        return "missing"
    return "downloading" if time.time() - path.stat().st_mtime < 30 else "ready"


def get_secret(name: str) -> str | None:
    """토큰·비밀번호는 settings.yaml 에 쓰지 않는다. 환경변수 → config/secrets.yaml 순서로 찾는다."""
    if value := os.environ.get(name):
        return value
    if SECRETS_FILE.exists():
        value = (yaml.safe_load(SECRETS_FILE.read_text(encoding="utf-8")) or {}).get(name)
        return None if value is None else str(value)
    return None


class Backend(BaseModel):
    base_url: str = "http://127.0.0.1:18080/v1"   # 로컬 주소면 포트는 runtime.port 를 따라간다
    api_key: str = "local"
    model: str = "qwen3-8b"          # llama-server 의 --alias 로도 쓰인다
    context_length: int = 16384
    timeout: float = 300
    # 외부 AI 모델 (agent/providers.py 가 실행마다 채운다). local = 이 PC 의 llama-server
    provider: Literal["local", "openai", "anthropic", "gemini"] = "local"
    effort: str = ""                 # 추론 강도 (외부 모델). 비우면 공급사 기본
    connection: str = ""             # 외부 모델 연결 id (실행 기록·사용량에 남긴다)
    profile: str = ""                # Claude 계정 로그인 프로필 (api_key 대신 — agent/providers.py)

    @property
    def external(self) -> bool:
        return self.provider != "local"


class Runtime(BaseModel):
    """관리 서버가 llama-server 를 직접 띄울 때의 설정 (agent/runtime.py)."""
    autostart: bool = True
    exe: str = "runtime/llama.cpp/llama-server.exe"
    model_path: str = "models/Qwen3-8B-Q4_K_M.gguf"
    host: str = "127.0.0.1"
    port: int = 18080                # 8080 은 Docker·개발 서버가 자주 써서 피한다. 쓰는 중이면 빈 포트로 자동 변경
    gpu_layers: int = 999            # 999 = 가능한 전부 GPU 에
    flash_attn: bool = True
    cache_type: str = "q8_0"         # KV 캐시 양자화. 비우면 f16
    extra_args: list[str] = Field(default_factory=list)


class Embedding(BaseModel):
    """문서 Q&A 의 의미 검색 (agent/embedding.py). llama-server 를 --embedding 으로 하나 더 띄운다 (처음 쓸 때).
    모델 파일이 없거나 꺼 두면 낱말 검색(FTS5)만 한다."""
    enabled: bool = True
    model_path: str = "models/Qwen3-Embedding-0.6B-Q8_0.gguf"
    host: str = "127.0.0.1"
    port: int = 18081
    gpu_layers: int = 0              # 0 = CPU — 채팅 모델과 VRAM 을 다투지 않는다 (0.6B 라 CPU 로도 충분)
    ctx: int = 4096                  # 한 조각(1200자 안팎)이 넉넉히 들어가게
    url: str = ""                    # 다른 곳에 이미 떠 있는 임베딩 서버를 쓸 때 (비우면 직접 띄운다)
    max_distance: float = 0.7        # 의미로만 찾은 조각의 코사인 거리 상한 (관계없는 문서까지 늘 나오지 않게)
    query_instruction: str = "Given a question, retrieve passages from the documents that answer the question"


class ModelPreset(BaseModel):
    """관리 화면에서 고를 수 있는 모델 하나. 고르면 runtime/backend 설정을 이 값으로 바꿔 llama-server 를 다시 띄운다."""
    name: str                          # llama-server --alias 겸 화면에 보이는 이름
    file: str                          # GGUF 경로
    note: str = ""
    context_length: int | None = None  # 비우면 backend.context_length
    gpu_layers: int | None = None
    extra_args: list[str] = Field(default_factory=list)   # 예) MoE: ["--n-cpu-moe", "36"]
    managed: bool = Field(default=False, exclude=True)     # connections.yaml 출신 (설정 화면에서 추가함)
    overridden: bool = Field(default=False, exclude=True)  # settings.yaml 의 모델인데 설정 화면에서 값을 바꿈


class Sampling(BaseModel):
    """모델 생성 파라미터. None 이면 보내지 않는다 (모델 서버 기본값)."""
    temperature: float = Field(default=0.6, ge=0, le=2)
    top_p: float = Field(default=0.95, gt=0, le=1)
    top_k: int | None = Field(default=None, ge=0)               # Qwen 권장 20 (llama-server 기본 40)
    min_p: float | None = Field(default=None, ge=0, le=1)        # Qwen 권장 0 (llama-server 기본 0.05)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)   # 같은 말 되풀이가 잦으면 0.5~1.5
    max_tokens: int | None = Field(default=None, ge=1)           # 한 번에 생성할 최대 토큰 (None = 한도 없음)


class AllowedDir(BaseModel):
    path: str
    mode: Literal["ro", "rw"] = "ro"


class Web(BaseModel):
    search_provider: Literal["ddgs", "searxng", "none"] = "ddgs"
    searxng_url: str = ""
    regions: list[str] = Field(default_factory=lambda: ["kr-kr", "us-en"])  # 지역마다 검색해서 섞는다
    max_results: int = 8          # 모든 지역을 합친 최대 결과 수
    fetch_timeout: float = 15
    max_fetch_bytes: int = 3_000_000
    allow_private_hosts: list[str] = Field(default_factory=list)  # 사내 사이트: 호스트 이름 또는 CIDR


class Auth(BaseModel):
    """요청 헤더에 넣을 인증. 실제 값(토큰·아이디·비밀번호)은 get_secret 으로 읽는다 (모델에게 보이지 않음).

    kind=token : secret 에 저장한 토큰을 header 에 prefix 를 붙여 넣는다.
    kind=login : username_secret·password_secret 의 계정으로 login_url 에 로그인(OAuth2 password, form)해서 받은
                 토큰을 넣는다. 토큰은 메모리에만 두고, 만료되거나 401 이면 다시 로그인한다 (agent/apiauth.py).
    """
    kind: Literal["token", "login"] = "token"
    header: str = "Authorization"
    secret: str = ""                  # kind=token: 환경변수 또는 config/secrets.yaml 의 키 이름
    prefix: str = ""                  # 예) "Bearer "
    login_url: str = ""               # kind=login: 토큰을 받는 주소 (문서의 tokenUrl)
    username_secret: str = ""         # kind=login: 아이디가 저장된 키 이름
    password_secret: str = ""         # kind=login: 비밀번호가 저장된 키 이름
    source: str = ""                  # 어디서 알게 됐는지 (예: 문서의 OAuth2 password, 문서의 로그인 주소가 localhost 라 바꿈)

    def missing(self) -> list[str]:
        """아직 저장되지 않은 값의 키 이름."""
        names = [self.username_secret, self.password_secret] if self.kind == "login" else [self.secret]
        return [n for n in names if not n or not get_secret(n)]


class ApiEndpoint(BaseModel):
    """api_get / api_send 로 호출할 수 있는 사내 API. 여기 등록된 것만 호출된다."""
    name: str
    title: str = ""                   # 사용자가 준 이름·문서 제목 그대로 (name 은 도구에 쓰는 짧은 영문·한글 이름)
    description: str = ""
    base_url: str
    auth: Auth | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    allow_methods: list[Literal["GET", "POST", "PUT", "PATCH", "DELETE"]] = Field(default_factory=lambda: ["GET"])
    docs: str = ""                    # 모델에게 보여줄 사용법 (엔드포인트 목록)
    docs_url: str = ""                # Swagger/OpenAPI 문서 주소. 있으면 docs 를 여기서 자동으로 만든다
    verify_tls: bool = True           # 사내 자체 서명 인증서면 false
    managed: bool = Field(default=False, exclude=True)   # connections.yaml 출신 (화면·채팅으로 등록됨)


class IntranetSource(BaseModel):
    """링크를 따라가며 수집해 search_intranet 으로 검색할 사내 웹사이트."""
    name: str
    description: str = ""
    start_urls: list[str]
    include_prefixes: list[str] = Field(default_factory=list)  # 이 주소로 시작하는 페이지만. 비우면 시작 주소의 사이트 전체
    exclude_patterns: list[str] = Field(default_factory=list)  # 주소에 이 글자가 들어 있으면 건너뜀 (예: /logout)
    max_pages: int = 300
    max_depth: int = 3
    delay: float = 0.2                # 페이지 사이 대기(초). 사내 서버 부담 줄이기
    auth: Auth | None = None
    verify_tls: bool = True
    managed: bool = Field(default=False, exclude=True)   # connections.yaml 출신 (화면·채팅으로 등록됨)

    def prefixes(self) -> list[str]:
        if self.include_prefixes:
            return self.include_prefixes
        return [f"{p.scheme}://{p.netloc}/" for p in map(urlsplit, self.start_urls)]


class Intranet(BaseModel):
    sources: list[IntranetSource] = Field(default_factory=list)


class Policy(BaseModel):
    """run_command 에 거는 규칙 (규격 '의존성·설치' 3장 — 프로젝트 로컬 설치만, 폐쇄망 전제)."""
    block_global_install: bool = True   # 시스템 전역 설치(venv 밖 pip, npm -g, dotnet tool -g, winget 등) 막기
    block_downloads: bool = True        # 인터넷 다운로드 명령(Invoke-WebRequest, curl, git clone 등) 막기
    require_env_check: bool = True      # 프로젝트에 패키지를 설치하기 전에 그 언어의 환경 확인 명령을 먼저 돌렸는지 보기


class Images(BaseModel):
    keep_days: int = 7   # 사내 API 에서 받은 원본 이미지를 실행 폴더에 두는 날수 (지나면 원본만 지우고 썸네일은 남긴다)


class Limits(BaseModel):
    max_steps: int = 20
    max_seconds: int = 900
    tool_output_chars: int = 4000
    command_timeout: int = 120


class Settings(BaseModel):
    backend: Backend = Field(default_factory=Backend)
    runtime: Runtime = Field(default_factory=Runtime)
    embedding: Embedding = Field(default_factory=Embedding)
    models: list[ModelPreset] = Field(default_factory=list)
    sampling: Sampling = Field(default_factory=Sampling)
    workspace: str = "workspace"
    session_folders: bool = True      # 대화마다 workspace 아래에 폴더를 만들고 그 안에서 작업 + 작업기록.md
    allowed_dirs: list[AllowedDir] = Field(default_factory=lambda: [AllowedDir(path="workspace", mode="rw")])
    web: Web = Field(default_factory=Web)
    apis: list[ApiEndpoint] = Field(default_factory=list)
    intranet: Intranet = Field(default_factory=Intranet)
    policy: Policy = Field(default_factory=Policy)
    limits: Limits = Field(default_factory=Limits)
    images: Images = Field(default_factory=Images)
    data_dir: str = "data"
    # 하네스 자료의 대상 회사 (실행마다 서버가 채운다 — 파일에 저장하지 않음). None = 전부 (관리자 · 혼자 쓰기),
    # 글자 = 그 회사가 대상인 폴더와 대상이 빈 폴더만 ('' = 회사 없는 사용자 → 대상이 빈 폴더만)
    audience: str | None = Field(default=None, exclude=True)
    # 입력창·워크플로 노드에서 고른 하네스 (harnesses/<이름> — '' = 공통만). 실행마다 서버가 채운다 (agent/harness.active)
    harness: str = Field(default="", exclude=True)
    # 입력창에서 고른 추론 강도 — 로컬 모델은 '생각' 켜기/끄기로 (None = 용도(프로필) 설정대로). 실행마다 서버가 채운다
    think: bool | None = Field(default=None, exclude=True)

    def apply_model(self, name: str) -> bool:
        """프리셋 이름으로 모델을 고른다. 없는 이름이면 아무것도 바꾸지 않고 False."""
        preset = next((m for m in self.models if m.name == name), None)
        if preset is None:
            return False
        self.backend.model = preset.name
        self.runtime.model_path = preset.file
        self.runtime.extra_args = list(preset.extra_args)
        if preset.context_length:
            self.backend.context_length = preset.context_length
        if preset.gpu_layers is not None:
            self.runtime.gpu_layers = preset.gpu_layers
        return True

    def sync_local_backend(self) -> None:
        """모델 서버를 이 PC 에서 띄우는 경우 backend 주소의 포트를 runtime.port 에 맞춘다 (포트가 자동으로 바뀌어도 일치)."""
        p = urlsplit(self.backend.base_url)
        if p.hostname in ("127.0.0.1", "localhost") and p.port != self.runtime.port:
            self.backend.base_url = f"{p.scheme}://{p.hostname}:{self.runtime.port}{p.path}"

    def registered_hosts(self) -> list[str]:
        """관리자가 등록한 사내 API·사이트의 호스트. 내부망 차단 규칙에서 자동으로 허용된다."""
        urls = [a.base_url for a in self.apis] + [a.auth.login_url for a in self.apis if a.auth and a.auth.login_url]
        for s in self.intranet.sources:
            urls += s.start_urls + s.include_prefixes
        return sorted({(urlsplit(u).hostname or "").lower() for u in urls} - {""})

    def path(self, p: str) -> Path:
        """설정 파일의 경로 문자열을 절대 경로로 바꾼다 (상대 경로는 프로젝트 폴더 기준)."""
        q = Path(p)
        return q if q.is_absolute() else ROOT / q

    @property
    def workspace_path(self) -> Path:
        return self.path(self.workspace)

    @property
    def data_path(self) -> Path:
        return self.path(self.data_dir)


def load_settings(path: Path | None = None) -> Settings:
    """settings.yaml (사람이 관리) + connections.yaml (화면·채팅으로 등록한 사이트·API) 를 합친다."""
    path = path or CONFIG_DIR / "settings.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    settings = Settings.model_validate(raw or {})
    conn = (yaml.safe_load(CONNECTIONS_FILE.read_text(encoding="utf-8")) or {}) if CONNECTIONS_FILE.exists() else {}
    # 설정 화면에서 추가한 모델, settings.yaml 모델에 화면에서 바꾼 값 (agent/model_presets.py)
    names = {m.name for m in settings.models}
    for item in conn.get("models") or []:
        preset = ModelPreset.model_validate(item)
        if preset.name not in names:   # 이름이 겹치면 settings.yaml 쪽이 우선
            preset.managed = True
            settings.models.append(preset)
    for name, changes in (conn.get("model_overrides") or {}).items():
        preset = next((m for m in settings.models if m.name == name and not m.managed), None)
        if preset is not None and isinstance(changes, dict):
            for key in ("note", "context_length", "gpu_layers", "extra_args"):
                if key in changes:
                    setattr(preset, key, changes[key])
            preset.overridden = True
    # 화면에서 고른 모델 > settings.yaml 의 backend.model (프리셋 이름이면 그 프리셋 값을 적용)
    settings.apply_model(conn.get("active_model") or settings.backend.model)
    if conn.get("runtime_port"):   # 기본 포트를 다른 프로그램이 써서 자동으로 바꾼 포트
        settings.runtime.port = int(conn["runtime_port"])
    settings.sync_local_backend()
    if conn:
        names = {s.name for s in settings.intranet.sources}
        for item in conn.get("intranet_sources") or []:
            src = IntranetSource.model_validate(item)
            if src.name not in names:   # 이름이 겹치면 settings.yaml 쪽이 우선
                src.managed = True
                settings.intranet.sources.append(src)
        names = {a.name for a in settings.apis}
        for item in conn.get("apis") or []:
            api = ApiEndpoint.model_validate(item)
            if api.name not in names:
                api.managed = True
                settings.apis.append(api)
        for rule in conn.get("allow_private_hosts") or []:   # 채팅·화면으로 허용한 내부 주소
            if rule not in settings.web.allow_private_hosts:
                settings.web.allow_private_hosts.append(rule)
    return settings


def managed_hosts() -> list[str]:
    """connections.yaml 에 저장된(= 화면·채팅으로 허용한) 내부 주소. 이것만 화면에서 지울 수 있다."""
    if not CONNECTIONS_FILE.exists():
        return []
    return list((yaml.safe_load(CONNECTIONS_FILE.read_text(encoding="utf-8")) or {}).get("allow_private_hosts") or [])
