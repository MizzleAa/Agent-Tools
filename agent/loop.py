"""★ 에이전트 루프 — 이 프로젝트의 핵심.

    messages = [시스템 프롬프트, 사용자 요청]
    반복:
        모델 호출(messages, 도구 목록)
        도구 호출이 없으면 → 최종 답변, 끝
        있으면 → 도구마다 (권한 확인 → 실행 → 결과를 messages 에 추가)
    한도(스텝·시간)에 걸리면 → 도구 없이 지금까지 결과로 답하게 한다

run() 은 제너레이터라서 진행 상황을 이벤트로 하나씩 내보낸다 (agent/events.py).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import urlsplit

from openai import APIConnectionError, APIError, BadRequestError
from pydantic import ValidationError

from agent import accounts, apiauth, codecheck, harness, connections, evidence, grounding, image_store, locations, openapi, prompts, session, standards
from agent.boundary import Boundary
from agent.config import Sampling, Settings
from agent.context import chars_of, fit_to_budget, leaked_note, truncate_output
from agent.directives import Directives, steps_missing
from agent.events import (ApprovalRequested, ApprovalResolved, Error, Event, Final, Images, LLMCall, Note, RunStarted,
                          ToolCall, ToolResult)
from agent.known_errors import KnownErrors
from agent.llm import LLMResponse, ToolCallRequest, assistant_message, leaked_call_syntax, make_client
from agent.providers import describe_error
from agent.profiles import Profile
from agent.safety import Sandbox, SandboxError, approval_reason, destructive_reason
from agent.session import LOG_NAME
from agent.tools import REGISTRY, ToolContext, ToolError
from agent.tools.api import _template as path_template
from agent.tools.shell import SHELL_NAME, failure_kind


class ChatModel(Protocol):
    model: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             think: bool | None = None) -> LLMResponse: ...


Approver = Callable[[ApprovalRequested], bool]

# 모델에게 가는 문구(시스템 프롬프트 블록, 루프 메시지, 도구 설명)는 agent/prompts.py 가 관리한다.
# 기본값은 agent/prompt_defaults/prompts.yaml, 관리 화면 '설정'에서 바꾼 것은 config/prompts.yaml.
NUDGE_ACT = prompts.default_message("nudge_act")   # 기본 문구 (테스트·비교용. 실제로는 설정에서 바꾼 문구를 쓴다)

# A2: 도구를 한 번도 안 쓰고 '하겠습니다' 같은 예고로 끝난 답 → 되돌려 보낸다
_FUTURE = re.compile(r"(겠습니다|겠어요|할게요|볼게요|드릴게요|줄게요|할 예정|하려고 합니다|진행합니다|시작합니다)"
                     r"|\b(I will|I'll|let me|I am going to|I'm going to)\b", re.I)
_ACTION = re.compile(r"(확인|설치|작성|만들|만드|생성|실행|구현|수정|진행|시작|테스트|빌드|저장|분석|정리|준비|살펴|찾아|"
                     r"코드|파일|폴더|프로젝트|create|write|install|check|build|run|implement|set up|start|look)", re.I)
_CONDITIONAL = re.compile(r"(으?면|다면|시면|경우|원하시|필요하시|말씀해)\s")
_QUESTION_END = re.compile(r"[?？]\s*$")

# A6: 같은 종류의 실패가 최근 실행 중 여러 번이면 경고한다 (R03 타임아웃 4회, R04 빌드 실패 5회 같은 패턴)
FAIL_WINDOW = 8
FAIL_WARN_AT = (3, 5)
FAIL_GUIDE = {"시간 초과": "fail_guide_timeout", "빌드 실패": "fail_guide_build"}   # 실패 종류 → 안내 메시지
CTX_RESERVE = 4096   # 컨텍스트 중 답변(생성)에 남겨 둘 토큰
GATE_MAX = 2         # 끝날 때 규격 점검으로 되돌려 보내는 최대 횟수
REFUSE_STOP = 5      # 같은 이유로 거부된 시도가 이만큼이면 멈춘다 (API-10)

CHANGING = ("write", "send", "admin")   # 상태를 바꾸는 도구 (같은 인자로 세 번이면 반복). 실행(exec)도 성공하면 상태가 바뀐 것으로 본다
INTRANET_TOOLS = {"api_get", "api_send", "add_api", "find_endpoint", "search_intranet", "add_intranet_site",
                  "crawl_site", "allow_host"}
PROMPT_DOC_LINES = 40   # API 하나당 시스템 프롬프트에 그대로 넣을 최대 엔드포인트 수. 넘으면 그룹 요약 + find_endpoint


def deny_all(_: ApprovalRequested) -> bool:
    return False


class Agent:
    def __init__(self, settings: Settings, profile: Profile, llm: ChatModel | None = None,
                 approver: Approver = deny_all, cancel: threading.Event | None = None,
                 run_id: str | None = None, max_steps: int | None = None, max_seconds: int | None = None,
                 workdir: Path | None = None, record: bool = True):
        """max_steps / max_seconds: 이번 실행에만 적용할 한도. 0 이면 무제한, None 이면 설정값.
        workdir: 이 대화(세션) 전용 폴더. 주면 상대 경로·명령 실행의 기준이 되고, 끝날 때 작업기록.md 를 남긴다.
        record: False 면 작업기록.md 를 남기지 않는다 (워크플로의 노드 — 워크플로가 한 번에 남긴다)."""
        self.record = record
        unknown = [t for t in profile.tools if t not in REGISTRY]
        if unknown:
            raise ValueError(f"프로필 '{profile.name}' 에 없는 도구가 있습니다: {unknown}. 사용 가능: {sorted(REGISTRY)}")
        self.settings = settings
        self.profile = profile
        self.prompts = prompts.load_prompts()   # 실행마다 새로 읽는다 → '설정'에서 바꾸면 다음 실행부터 바로 적용
        self.llm = llm or make_client(settings.backend, profile_sampling(settings.sampling, profile))
        self.approver = approver
        self.cancel = cancel or threading.Event()
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.tools = {name: REGISTRY[name] for name in profile.tools}
        self._scopes = harness.active(settings.audience, settings.harness)   # 공통 + 회사 하네스 + 고른 하네스
        self._knowledge = harness.has_docs(settings.audience, self._scopes)   # 하네스 자료가 있으면 어느 용도든 찾기·읽기 도구를 붙인다
        if self._knowledge:
            for name in ("search_knowledge", "read_knowledge"):
                self.tools.setdefault(name, REGISTRY[name])
        self.sandbox = Sandbox.from_settings(settings)
        self.workdir = workdir
        if workdir is not None:
            self.sandbox.resolve(str(workdir), write=True)   # 허용된 쓰기 폴더 안이어야 한다
            workdir.mkdir(parents=True, exist_ok=True)
            self.sandbox.cwd = workdir
        self.ctx = ToolContext(settings, self.sandbox, settings.data_path / "runs" / self.run_id, self.cancel)
        self.tainted = False  # 외부 내용을 읽었는지 (safety.approval_reason 참고)
        self.messages: list[dict[str, Any]] = []
        limits = settings.limits
        self.max_steps = max_steps if max_steps is not None else (
            profile.max_steps if profile.max_steps is not None else limits.max_steps)
        self.max_seconds = max_seconds if max_seconds is not None else limits.max_seconds
        self._seen: dict[str, int] = {}   # 반복 감지: 호출 키 → 연속 횟수
        self._version = 0                 # 상태를 바꿀 수 있는 도구(쓰기·실행)가 성공할 때마다 +1
        self._after: dict[str, int] = {}  # 읽기·실행 호출 키 → 그 호출 직후의 _version (그 사이 다른 변화가 있었는지 본다)
        self._ledger: list[evidence.Step] = []   # 이번 실행의 도구 기록 (최종 보고를 이 기록과 대조한다)
        self._task = ""
        self._reviewed: tuple[str, evidence.Review] | None = None
        self._no_tools_nudged = False     # D1: 도구 없이 한 일을 보고해서 되돌려 보냈는지
        self._boundary: Boundary | None = None   # E1: 대화 작업 폴더 경계 (세션 폴더가 있을 때)
        self.known_errors = KnownErrors()  # E6: 이 대화에서 '없는 이름'으로 실패한 API (이어서 하기면 이전 실행 것도)
        self._unfinished = 0              # E2: 끊긴 답이라 되돌려 보낸 횟수
        self._cred_nudged = False         # API-4: 채팅으로 자격 증명을 요구해서 되돌려 보냈는지
        self._v1_hinted = False           # E11: 문법 검사만 했을 때 V1 ②③ 안내를 했는지 (한 번만)
        self._refusals: dict[str, int] = {}   # API-10: 거부 이유별 횟수
        self._handoff_nudged = False      # API-11: 사용자가 못 하는 일을 넘겨서 되돌려 보냈는지
        self._directives = Directives()   # S-2·S-3: 사용자가 준 지시 원문·금지·단계 (실행 시작 때 사용자 말로 채운다)
        self._given: list[str] = []       # S-5: 사용자가 준 파일 (실행 시작 때 이미 있던 것)
        self._plan_end_nudged = False     # S-4: 도구를 쓴 뒤 '…하겠습니다' 로 끝난 답을 되돌려 보냈는지
        self._steps_nudged = False        # S-4: 번호 매긴 단계의 결과가 답에 다 있는지 되돌려 보냈는지
        self._unlock_nudged = False       # S-3: 조회만인데 변경 허용을 켜 달라고 해서 되돌려 보냈는지
        self._han_nudged = False          # API-18: 한자 섞인 답을 고쳐 쓰게 했는지
        self._finish = ""                 # 마지막 모델 응답의 finish_reason
        self._loop = False
        self._tpc = 0.6                   # 글자당 토큰 추정치. 실제 prompt_tokens 로 계속 고친다 (처음엔 넉넉하게)
        self._sent_chars = 0
        self._nudged = 0                  # A2: 예고만 하고 끝나려 해서 되돌려 보낸 횟수
        self._gated = 0                   # B1·B2: 끝날 때 규격 점검으로 되돌려 보낸 횟수
        self._fails: deque[str | None] = deque(maxlen=FAIL_WINDOW)   # A6: 최근 실행 결과의 실패 종류
        self._lang: str | None = None     # B2: 개발 중인 언어 (그 언어 규격을 시스템 프롬프트에 붙인다)
        self._dev = False                 # 이번 실행에서 코드·프로젝트 파일을 만들었는지 (개발 작업인지)
        self._corpus = grounding.Seen()   # S-7·S-8: 이번 실행에서 실제로 본 글 (사용자 말·지침·성공한 도구 결과)
        self._digests: dict[str, str] = {}   # S-7: 도구 호출 id → 결과 한 줄 요약 (오래된 기록을 지울 때 남긴다)
        self._written: set[str] = set()   # 이번 실행에서 쓴 파일 (다시 읽어도 '받은 결과'로 치지 않는다)
        self._grounded: tuple[str, list[str]] | None = None
        self._grounding_nudged = 0        # S-7: 보고가 실행 기록과 맞지 않아 되돌려 보낸 횟수
        self._summary_nudged = False      # S-11: 'N줄 요약' 단계의 요약이 없어 되돌려 보냈는지
        self._wants_images = False        # 이미지를 보여 달라는 요청인지 (IMG-2·DONE-1)
        self._image_nudged = False        # IMG-2: 이미지 요청인데 이미지 경로를 한 번도 부르지 않아 되돌려 보냈는지
        self._try_nudged = False          # LONG-1: 도구 없이 '못 한다·알려 주시면' 으로 넘겨서 되돌려 보냈는지
        self._touched: list[str] = []     # 도구에 넘긴 절대 위치 (공유 기능 T4·T6: 적힌 위치를 열어 보지 않고 짐작한 답 막기)
        self._loc_nudged = False

    # ───────────────────────── 프롬프트 조립 (agent/prompts.py)

    def variables(self) -> dict[str, str]:
        """{{변수}} 값. 내장 변수 > 프로필 변수 > 전역 변수."""
        today = date.today()
        builtin = {
            "cwd": str(self.sandbox.cwd), "session_folder": str(self.workdir) if self.workdir else "",
            "log_name": LOG_NAME, "dirs": self.sandbox.describe(), "shell": SHELL_NAME,
            "today": today.isoformat(), "weekday": "월화수목금토일"[today.weekday()] + "요일",
            "profile": self.profile.name, "model": self.llm.model,
            "context_length": str(self.settings.backend.context_length), "tools": ", ".join(self.tools),
        }
        return {**self.prompts.values(), **self.profile.variables, **builtin}

    def prompt_sections(self) -> list[tuple[str, str, str]]:
        """(블록 id, 제목, 내용) — 시스템 프롬프트를 이루는 조각들. 비는 조각은 빠진다 (미리보기도 이걸 쓴다)."""
        values, flags = self.variables(), set(self.tools)
        out = [("role", "역할", prompts.render(self.profile.system_prompt, values, flags).strip())]
        out += self._harness_sections()
        for bid in self.profile.effective_blocks():
            block = self.prompts.blocks.get(bid)
            if block is None:
                continue   # 지워진 블록 이름이 프로필에 남아 있으면 건너뛴다 (미리보기에서 경고)
            if bid == "intranet_status":
                text = self._intranet_context()
            elif bid == "lang_standard":
                text = self._lang_section(self._lang) if self.profile.standards and self._lang else ""
            else:
                text = prompts.render(block.text, values, flags)
            if text.strip():
                out.append((bid, block.title, text.strip()))
        d = self._directives   # S-2·S-3: 사용자가 준 지시 원문과 금지 — 압축되지 않는 시스템 지침에 둔다
        if d.pinned or d.read_only:
            materials = "\n\n".join(f"### {src}\n{text}" for src, text in d.pinned) or "(지시 파일 없음)"
            limit = self._msg("read_only_line", quote=d.read_only) if d.read_only else ""
            out.append(("user_directives", "사용자가 준 지시", self._msg("directives_block", materials=materials, limit=limit)))
        return out

    def _harness_sections(self) -> list[tuple[str, str, str]]:
        """하네스(관리자가 마크다운으로 관리 — agent/harness.py): 공통 지침 · 작업 폴더의 지침 파일 · 자료 목록.
        16k 급 로컬 모델이면 짧게 자른다 (자료 본문은 넣지 않고 도구로 꺼내 읽게 한다)."""
        small = self.settings.backend.context_length <= 32768
        budget, items = (2500, 30) if small else (12000, 150)
        out = []
        if text := harness.instructions(self.settings.audience, budget, self._scopes):
            out.append(("harness", "공통 지침 (하네스)", text))
        if text := harness.project_instructions(self.workdir or self.sandbox.cwd, self.settings.workspace_path, budget):
            out.append(("project_instructions", "작업 폴더의 지침 파일", text))
        if self._knowledge:
            out.append(("knowledge", "하네스 자료", self._msg("knowledge_block", catalog=harness.catalog(self.settings.audience, items, self._scopes))))
        return out

    def system_prompt(self) -> str:
        return "\n\n".join(text for _, _, text in self.prompt_sections())

    def _hidden_tools(self) -> set[str]:
        """이번 실행에서 모델에게 보여 주지 않는 도구: 사용자가 변경을 금지했으면 변경 도구(S-3),
        수집한 사내 사이트가 없으면 사이트 검색·재수집(S-6: 쓸 수 없는 도구를 불러 스텝 낭비)."""
        hidden = {n for n, t in self.tools.items() if t.permission == "send"} if self._directives.read_only else set()
        if not self.settings.intranet.sources:
            hidden |= {"search_intranet", "crawl_site"}
        return hidden

    def _visible_schemas(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hidden = self._hidden_tools()
        return [s for s in schemas if s["function"]["name"] not in hidden]

    def tool_schemas(self) -> list[dict[str, Any]]:
        """모델에게 보여 줄 도구 목록. '설정'에서 바꾼 도구 설명이 있으면 그걸 쓴다."""
        values, flags = self.variables(), set(self.tools)
        out = []
        for t in self.tools.values():
            schema = t.schema()
            if t.name in self.prompts.tools:
                schema["function"]["description"] = prompts.render(self.prompts.tools[t.name], values, flags).strip()
            out.append(schema)
        return out

    def _msg(self, key: str, **extra: Any) -> str:
        return self.prompts.message(key, self.variables(), self.tools, **extra)

    def _lang_section(self, lang: str) -> str:
        return self._msg("lang_header", label=standards.LANG_LABEL[lang]) + "\n" + standards.load(lang).strip()

    def _intranet_context(self) -> str:
        """지금 등록된 사내 연결 현황. 없으면 '없음'까지 적어서, 모델이 '등록된 API 있어?' 에 이 목록으로 답하게 한다."""
        if not INTRANET_TOOLS & self.tools.keys():
            return ""
        s = self.settings
        out = ["\n## 사내 연결 현황 (사용자가 등록된 API·사이트·허용 주소를 물으면 검색하지 말고 이 목록으로 답한다)"]
        out.append("### 사내 API (api_get / api_send 로만 호출. 여기 없는 API 는 부를 수 없다)")
        if not s.apis:
            out.append("- (등록된 API 없음. Swagger 문서 주소를 받으면 add_api 로 등록한다)")
        for a in s.apis:
            title = f" ({a.title})" if a.title and a.title not in a.description else ""   # 사용자가 준 이름 (설명에 이미 있으면 생략)
            out.append(f"- {a.name}{title}: {a.description} · 기본 주소 {a.base_url} · "
                       f"허용 {', '.join(a.allow_methods)}" + (f" · 문서 {a.docs_url}" if a.docs_url else ""))
            out.append(f"    인증: {apiauth.describe(a)}")
            lines = a.docs.strip().splitlines()
            if len(lines) <= PROMPT_DOC_LINES:
                out += [f"    {line}" for line in lines]
            else:   # 엔드포인트가 많으면 경로 그룹 요약만 넣고, 필요한 건 find_endpoint 로 찾게 한다 (컨텍스트 절약)
                out.append(f"    엔드포인트 {len(lines)}개 — 경로 그룹: {openapi.path_groups(a.docs)}")
                out.append(f"    필요한 경로는 find_endpoint(api='{a.name}', query='영어 키워드') 로 찾는다. 경로를 지어내지 않는다.")
            if self._wants_images and (shown := image_store.image_paths(a.docs, 6)):   # IMG-2: 이미지 요청이면 이미지 경로를 바로 보이게
                out.append("    이미지 자체를 돌려주는 조회 경로 (문서에서 뽑음 — api_get 하면 채팅 화면에 붙는다. 사용자가 파일 이름을 줬으면 "
                           "이름을 넣는 경로를 그 이름 그대로 바로 부른다. 없으면 화면에 '찾을 수 없음'):")
                out += [f"      {line[:150]}" for line in shown]
        out.append("### 사내 사이트 (search_intranet 의 source 이름)")
        out += [f"- {src.name}: {src.description} · {src.prefixes()[0]}" for src in s.intranet.sources] \
            or ["- (등록된 사이트 없음)"]
        hosts = sorted(set(s.web.allow_private_hosts) | set(s.registered_hosts()))
        out.append("### 읽기 허용된 내부 주소: " + (", ".join(hosts) if hosts else "(없음. 차단되면 allow_host 로 허용)"))
        return "\n".join(out)

    def run(self, task: str, history: list[dict[str, Any]] | None = None,
            parent_run_id: str = "") -> Iterator[Event]:
        """history: 이어서 하기일 때 이전 실행의 대화 (history_from_events 로 만든다).
        세션 폴더가 있으면 실행이 끝난 뒤 작업기록.md·작업목록.md 를 정리한다."""
        events: list[dict[str, Any]] = []
        for ev in self._run(task, history, parent_run_id):
            events.append(ev.to_dict())
            yield ev
        if self.workdir is not None and self.record:
            try:
                session.record_run(self.settings.workspace_path, self.workdir, events)
            except OSError as e:   # 기록 실패가 작업 결과를 망치지 않게
                print(f"작업기록을 남기지 못했습니다: {e}")

    def _run(self, task: str, history: list[dict[str, Any]] | None, parent_run_id: str) -> Iterator[Event]:
        self._task = task
        self._wants_images = bool(IMAGE_ASK.search(task)) and "api_get" in self.tools
        if self.workdir is not None:
            self._boundary = Boundary(self.settings.workspace_path, self.workdir, task)
        schemas = self.tool_schemas()
        if self.profile.standards and self.workdir is not None:
            self._lang = standards.detect_from_folder(self.workdir)   # 이어서 하는 대화면 이미 쓰던 언어의 규격을 붙인다
        self._directives = Directives.from_user([str(m.get("content") or "") for m in history or []
                                                 if m.get("role") == "user"] + [task])
        self._given = self._given_files()
        self.messages = [{"role": "system", "content": self.system_prompt()}, *(history or []),
                         {"role": "user", "content": task}]
        for m in self.messages:   # S-8: 사용자 말·지침·이어서 한 대화에 있던 값은 '본 것'
            self._corpus.add(str(m.get("content") or ""))
        self.ctx.memo["seen"] = self._corpus
        started = time.monotonic()
        totals = {"prompt": 0, "completion": 0}
        used: list[str] = []

        def final(status: str, answer: str, steps: int) -> Final:
            return Final(status, answer, steps, totals["prompt"], totals["completion"],
                         round(time.monotonic() - started, 2), used)

        yield RunStarted(self.run_id, task, self.profile.name, self.llm.model, parent_run_id,
                         str(self.workdir) if self.workdir else "")

        step = 0
        status = "max_steps"
        while self.max_steps <= 0 or step < self.max_steps:   # 0 = 무제한
            if self.cancel.is_set():
                yield final("cancelled", "사용자가 실행을 중지했습니다.", step)
                return
            if 0 < self.max_seconds < time.monotonic() - started:
                status = "timeout"
                break
            if self._loop:
                status = "loop"
                break
            step += 1

            # 1) 모델 호출
            try:
                t0 = time.monotonic()
                resp = self._chat(self._visible_schemas(schemas))
            except APIConnectionError as e:
                if self.settings.backend.external:   # 외부 AI 모델: 인터넷·공급사 쪽 문제 (B5)
                    msg = f"외부 모델({self.settings.backend.provider} · {self.llm.model}) 호출 실패: {describe_error(e)}"
                else:
                    msg = f"모델 서버({self.settings.backend.base_url})에 연결할 수 없습니다. 관리 화면 '상태' 탭에서 모델 서버를 켜세요."
                yield Error(msg)
                yield final("error", msg, step)
                return
            except APIError as e:
                why = describe_error(e) if self.settings.backend.external else str(e)
                yield Error(f"모델 호출 실패: {why}")
                yield final("error", f"모델 호출 실패: {why}", step)
                return
            totals["prompt"] += resp.prompt_tokens
            totals["completion"] += resp.completion_tokens
            yield LLMCall(step, resp.content, resp.reasoning,
                          [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in resp.tool_calls],
                          resp.prompt_tokens, resp.completion_tokens, round(time.monotonic() - t0, 2), resp.finish_reason)
            self.messages.append(assistant_message(resp))
            self._finish = resp.finish_reason

            # 2) 도구 호출이 없으면 최종 답변 — 단, 예고만 했거나(A2) 규격 점검에 걸리면(B1·B2) 한 번 더 일하게 한다
            if not resp.tool_calls:
                nudge = self._nudge_for(resp.content, used)
                if nudge:
                    kind, why, text = nudge
                    self.messages.append({"role": "user", "content": text})
                    yield Note(step, kind, why, text)
                    continue
                status = self._final_status(resp.content, used)
                yield final(status, self._annotate(resp.content, used, status), step)
                return

            # 3) 도구를 하나씩 실행하고 결과를 기록에 추가
            for call in resp.tool_calls:
                if self.cancel.is_set():
                    break
                t0 = time.monotonic()
                ok, output = yield from self._execute(step, call)
                used.append(call.name)
                yield ToolResult(step, call.id, call.name, ok, output, round(time.monotonic() - t0, 2))
                if shown := self.ctx.memo.pop("images_new", None):   # 화면에 붙일 이미지 (모델 입력에는 넣지 않는다)
                    yield Images(step, call.id, shown)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

        # 4) 한도 도달 / 반복 감지: 도구 없이 마무리 답변을 받는다
        self.messages.append({"role": "user", "content": self._msg("force_final_loop" if status == "loop" else "force_final")})
        try:
            resp = self._chat(None, think=False)
            totals["prompt"] += resp.prompt_tokens
            totals["completion"] += resp.completion_tokens
            answer = resp.content
        except APIError as e:
            answer = f"(마무리 답변 실패: {e})"
        yield final(status, self._annotate(answer, used, status), step)   # 한도로 끝난 보고도 실행 기록과 대조 (E9)

    # ───────────────────────── A2·B1·B2: 끝내기 전에 한 번 더 일하게 할지

    def _net_tools(self) -> bool:
        """사내 API·웹을 다루는 용도인지 (API-4 자격 증명 요청 점검은 여기에만 — 코드 TODO 의 '비밀번호' 같은 오탐을 피한다).
        fetch_url 하나만 있는 용도(general: 공유 링크·문서 파일 읽기)는 파일 비서라 여기에 넣지 않는다."""
        return bool(INTRANET_TOOLS & self.tools.keys() or {"web_search"} & self.tools.keys())

    def _has_action_tools(self) -> bool:
        return any(t.permission in ("write", "exec", "send", "admin") for t in self.tools.values())

    def _review(self, answer: str, used: list[str]) -> evidence.Review:
        """최종 답을 이번 실행 기록·작업 폴더와 대조한다 (D1·D2·D4·D10·D11). 같은 답이면 다시 하지 않는다."""
        if self._reviewed and self._reviewed[0] == answer:
            return self._reviewed[1]
        if not self._has_action_tools():   # 읽기 전용 용도: 한 일을 지어낼 수 없으니 글자만 본다
            rv = evidence.Review(evidence.check_language(answer, self._task))
        else:
            dev = self.profile.standards or evidence.is_report(answer)
            rv = evidence.review(answer, self._task, self._ledger, self.workdir or self.sandbox.cwd,
                                 used_any_tool=bool(used), dev_report=dev)
        self._reviewed = (answer, rv)
        return rv

    def _nudge_for(self, answer: str, used: list[str]) -> tuple[str, str, str] | None:
        """(종류, 화면에 보일 설명, 모델에게 줄 말) 또는 None(그대로 끝냄)."""
        if not used and self._has_action_tools() and looks_like_plan(answer) and self._nudged < 2:
            self._nudged += 1
            return "act", "계획만 말하고 끝내려 해서 실제로 실행하라고 되돌려 보냄", self._msg("nudge_act")
        if self._net_tools() and asks_for_credentials(answer) and not self._cred_nudged:   # API-4: 채팅으로 비밀번호·토큰을 요구하지 않는다
            self._cred_nudged = True
            return "credentials", "채팅으로 비밀번호·토큰을 요구해서 관리 화면 안내로 고치라고 되돌려 보냄", self._msg("no_credentials")
        if looks_unfinished(answer, self._finish) and self._unfinished < 2:   # E2: 끊긴 답을 최종 답으로 받지 않는다
            self._unfinished += 1
            return "unfinished", "답이 문장 중간에 끊겨(도구 호출이 깨졌을 수 있음) 이어서 하라고 되돌려 보냄", self._msg("unfinished")
        if self._directives.read_only and _UNLOCK.search(answer) and not self._unlock_nudged:   # S-3
            self._unlock_nudged = True
            return "read_only", "변경을 금지한 작업인데 변경 허용을 켜 달라고 해서 되돌려 보냄", \
                self._msg("read_only_answer", quote=self._directives.read_only)
        if used and ends_with_plan(answer) and not self._plan_end_nudged:   # S-4: '이제 … 하겠습니다' 로 끝난 답
            self._plan_end_nudged = True
            return "act", "도구를 쓴 뒤 '…하겠습니다' 로 끝내려 해서 남은 일을 하거나 결과로 끝맺으라고 되돌려 보냄", \
                self._msg("plan_ending")
        if not used and self._net_tools() and _ASK_BACK.search(answer) and not asks_for_credentials(answer) \
                and not self._try_nudged:
            self._try_nudged = True   # LONG-1: 도구 0회로 '못 한다 · 알려 주시면' — 시도할 경로가 있으면 먼저 시도한다
            return "act", "도구를 한 번도 쓰지 않고 할 수 없다고(알려 달라고) 답해서 먼저 시도하라고 되돌려 보냄", self._msg("try_first")
        if (untried := self._untried_locations()) and not self._loc_nudged:
            self._loc_nudged = True   # 공유 기능 T4·T6: 적힌 위치를 열어 보지 않고 '없다·못 연다'고 짐작한 답
            return "act", f"작업에 적힌 위치 {len(untried)}곳을 도구로 열어 보지 않고 답해서 먼저 시도하라고 되돌려 보냄", \
                self._msg("try_locations", paths="\n".join(f"- {p}" for p in untried))
        if self._wants_images and not self.ctx.memo.get("images") and not self._image_nudged:
            self._image_nudged = True   # IMG-2: 이미지를 보여 달라는데 이미지 경로를 한 번도 부르지 않았다
            return "act", "이미지를 보여 달라는 요청인데 이미지를 한 번도 받아 붙이지 않아 이미지 경로로 시도하라고 되돌려 보냄", \
                self._msg("image_try", paths=self._image_path_list() or "  (문서에서 못 찾음 — find_endpoint 로 image thumbnail preview 를 찾는다)")
        if (wrong := self._grounding(answer)) and self._grounding_nudged < GATE_MAX:   # S-7: 받은 결과와 맞지 않는 보고
            self._grounding_nudged += 1
            return "grounding", f"보고의 경로·값 {len(wrong)}가지가 이번 실행의 도구 결과와 맞지 않아 실제 결과와 함께 되돌려 보냄", \
                self._msg("grounding", problems="\n".join(f"- {p}" for p in wrong),
                          results=grounding.results_summary(self._ledger, focus=grounding.flagged_items(wrong))
                          or "- (성공한 API 조회 없음)")
        missing, summaries = self._step_gaps(answer)
        if missing and not self._steps_nudged:
            self._steps_nudged = True   # S-4: 번호 매긴 단계 중 답에 결과가 없는 단계
            return "steps", f"지시의 단계 {', '.join(map(str, missing))} 결과가 답에 없어 되돌려 보냄", \
                self._msg("steps_missing", steps=f"1~{self._directives.steps[-1]}", missing=", ".join(map(str, missing)))
        if summaries and not self._summary_nudged:   # S-11: '세 줄로 요약' 단계에 '요약 제공'이라고만 씀
            self._summary_nudged = True
            n, lines = summaries[0]
            return "steps", f"지시의 {n}단계({lines}줄 요약)의 요약이 답에 없어 되돌려 보냄", \
                self._msg("summary_missing", step=n, ask=self._directives.texts.get(n, "")[:80], lines=lines)
        if (handoff := self._handoff(answer)) and not self._handoff_nudged:   # API-11: 사용자가 못 하는 일을 넘기지 않는다
            self._handoff_nudged = True
            return "handoff", "사용자가 할 수 없는 일(스크립트·명령 실행)을 다음 단계로 넘겨서 되돌려 보냄", \
                self._msg("user_cannot", reason=handoff)
        if self._net_tools() and (han := evidence.check_language(answer, self._task)) and not self._han_nudged:
            # API-18: 사내 API·웹 답에 한자가 섞이면 한 번 고쳐 쓰게 한다 (다른 용도는 D11 대로 '(참고)' 표시만)
            self._han_nudged = True
            return "language", "답에 사용자가 쓰지 않은 문자(한자)가 섞여 고쳐 쓰라고 되돌려 보냄", \
                self._msg("rewrite_language", detail=han[0].text.split(":", 1)[-1].split("—")[0].strip())
        rv = self._review(answer, used)
        if rv.no_tools:   # D1: 도구 0회인데 파일·실행·검증을 보고 → 한 번 되돌려 보내고, 또 그러면 '실행 안 함'
            if self._no_tools_nudged:
                return None
            self._no_tools_nudged = True
            return "evidence", "도구를 한 번도 쓰지 않고 한 일을 보고해서 실제로 하라고 되돌려 보냄", self._msg("no_evidence")
        dev = (self.profile.standards and self._dev) or evidence.is_report(answer)
        problems = [f.text for f in rv.findings if f.blocks_done or dev]   # (참고)는 개발 보고일 때만 고치라고 되돌린다
        lang_note = ""
        folder = self.workdir or self.sandbox.cwd
        if self.profile.standards and self._dev:
            spec = standards.check_project(folder, self._lang) + standards.check_report(answer)
            problems = spec + problems
            if spec and not self._lang:
                lang_note = "\n" + self._msg("gate_no_lang")
        if problems and self._boundary is not None and (outside := self._boundary.summary()):
            problems.append(outside)   # E3: 점검이 어디를 봤고 작업 폴더 밖에서 무엇을 했는지
        if problems and self._gated < GATE_MAX:
            self._gated += 1
            listed = "\n".join(f"- {p}" for p in problems)
            where = self._boundary.where() if self._boundary is not None else f"'{folder}' (명령의 현재 폴더 '.')"
            text = self._msg("gate", problems=listed, lang_note=lang_note, where=where)
            return "gate", f"규격·실행 기록 점검에서 {len(problems)}가지가 걸려 되돌려 보냄", text
        return None

    def _final_status(self, answer: str, used: list[str]) -> str:
        has_action = self._has_action_tools()
        if not used and has_action and (looks_like_plan(answer) or self._review(answer, used).no_tools):
            return "no_action"   # 실행하지 않았다 (예고만 했거나 한 일을 지어냄) → '완료'로 기록하지 않는다
        if looks_unfinished(answer, self._finish):
            return "partial"     # E2: 끊긴 답은 되돌려 보내기를 다 써도 완료가 아니다
        status = "done"
        if self.profile.standards and (self._dev or evidence.is_report(answer)):
            result = standards.report_result(answer)
            status = {"부분 완료": "partial", "실패": "failed"}.get(result or "", "done")
            if status == "done" and self._dev and (result is None or standards.check_report(answer) or
                                                   standards.check_project(self.workdir or self.sandbox.cwd, self._lang)):
                status = "partial"   # E2: 개발 작업인데 보고 형식이 아니거나 규격에 못 미친 채 끝남
        if status == "done" and self._review(answer, used).blocking:
            status = "partial"   # 실행 기록으로 확인되지 않는 주장이 남아 있다
        if status == "done" and not (self.profile.standards and self._dev) and (unanswered_part(answer) or self._handoff(answer)):
            status = "partial"   # API-13: 요청한 것 중 답하지 못했거나 사용자가 못 하는 일로 넘겼다 (개발 보고가 아닌 답도)
        if status == "done" and ((used and ends_with_plan(answer)) or any(self._step_gaps(answer))):
            status = "partial"   # S-4·S-11: 할 일을 예고하며 끝났거나, 지시한 단계의 결과(요약 포함)가 답에 없다
        if status == "done" and self._grounding(answer):
            status = "partial"   # S-7: 보고의 근거·값이 이번 실행의 도구 결과와 맞지 않는다
        if status == "done" and (self._no_image_shown() or self._no_response()):
            status = "partial"   # DONE-1: 요청한 결과(이미지)가 하나도 안 나왔거나, 도구가 응답을 하나도 받지 못했다
        return status

    def _no_image_shown(self) -> bool:
        """이미지를 보여 달라고 했는데 화면에 제대로 붙은 이미지가 한 장도 없다."""
        return self._wants_images and not any(e.get("ok") for e in self.ctx.memo.get("images", []))

    def _no_response(self) -> bool:
        """사내 API·웹 도구를 썼는데 모두 실패했고 서버의 응답(HTTP 상태)도 하나 없다 (시간 초과·차단뿐 — QA I03)."""
        net = [st for st in self._ledger if st.name in INTRANET_TOOLS | {"fetch_url", "web_search"} and st.name != "find_endpoint"]
        return bool(net) and not any(st.ok or "→ HTTP " in (st.output or "") for st in net)

    def _image_path_list(self) -> str:
        return "\n".join(f"  [{a.name}] {line[:150]}" for a in self.settings.apis for line in image_store.image_paths(a.docs, 6))

    def _annotate(self, answer: str, used: list[str], status: str) -> str:
        """점검에서 끝까지 남은 것을 답 끝에 붙인다 — 사용자가 보고를 그대로 믿지 않게."""
        rv = self._review(answer, used)
        findings = list(rv.findings)
        if self._net_tools() and asks_for_credentials(answer):
            findings.insert(0, evidence.Finding("비밀번호·토큰은 채팅으로 보내지 마세요. 관리 화면 '상태 · 연결' › 사내 API › 그 API 의 "
                                                "인증 칸에 넣으면 에이전트가 직접 보지 않고 로그인·인증에 씁니다", False))
        if looks_unfinished(answer, self._finish):
            findings.insert(0, evidence.Finding("답이 문장 중간에 끊겼습니다 (모델 응답이 도중에 멈춤 — 하려던 일을 하지 않고 끝남)"))
        if status == "partial" and not (self.profile.standards and self._dev) and (part := unanswered_part(answer)):
            findings.insert(0, evidence.Finding(f"요청한 것 중 답하지 못한 부분이 있어 '부분 완료'로 기록했습니다: '{part[:80]}'"))
        if handoff := self._handoff(answer):
            findings.insert(0, evidence.Finding(f"사용자가 할 수 없는 일을 다음 단계로 안내했습니다 ({handoff[:60]}) — 그 단계는 "
                                                "건너뛰고, 필요하면 담당자에게 요청하세요", False))
        missing, summaries = self._step_gaps(answer)
        if missing:
            findings.insert(0, evidence.Finding(f"지시한 단계 중 답에 결과가 없는 단계: {', '.join(map(str, missing))}"))
        for n, lines in summaries:
            findings.insert(0, evidence.Finding(f"지시한 {n}단계의 {lines}줄 요약이 답에 없습니다 ('요약했다'는 말만 있음)"))
        for problem in self._grounding(answer):
            findings.insert(0, evidence.Finding(problem))
        if self._no_image_shown():
            failed = [e for e in self.ctx.memo.get("images", []) if not e.get("ok")]
            findings.insert(0, evidence.Finding("이미지를 보여 달라고 했지만 화면에 표시된 이미지가 없습니다" +
                                                (f" ('찾을 수 없음' {len(failed)}장)" if failed else " (이미지를 받아 오지 않음)")))
        if self._no_response():
            findings.insert(0, evidence.Finding("이번 실행의 사내 API·웹 호출이 모두 실패해 응답을 하나도 받지 못했습니다 (시간 초과·차단 등) — "
                                                "요청한 결과가 아닙니다"))
        findings += self._wrong_paths(answer)
        if used and ends_with_plan(answer):
            findings.insert(0, evidence.Finding("답이 할 일을 예고하며('…하겠습니다') 끝났습니다 — 그 일은 하지 않았습니다"))
        if self._directives.read_only and _UNLOCK.search(answer):
            findings.insert(0, evidence.Finding(f"사용자가 변경을 금지했습니다('{self._directives.read_only[:60]}'). 변경 허용을 켤 "
                                                "필요가 없습니다 — 조회로 한 결과만 보세요", False))
        if not findings:
            return answer
        items = "\n".join(f"> - {'' if f.blocks_done else '(참고) '}{f.text}" for f in findings[:8])
        return answer.rstrip() + "\n\n" + self._msg("evidence_note", items=items, status=status)

    # ───────────────────────── A1: 컨텍스트 한도 안으로 맞추기

    def _budget_tokens(self) -> int:
        ctx = self.settings.backend.context_length
        return ctx - min(CTX_RESERVE, ctx // 4)

    def _fit(self, schemas: list[dict[str, Any]] | None) -> None:
        """보낼 요청이 한도를 넘을 것 같으면 미리 줄인다. 토큰 수는 '글자 수 × 실제로 잰 글자당 토큰'으로 어림한다."""
        limit = self._budget_tokens()
        if chars_of(self.messages, schemas) * self._tpc > limit:
            fit_to_budget(self.messages, schemas, int(limit * 0.8 / self._tpc), self._digests)   # 매번 줄이지 않게 여유를 두고
        self._sent_chars = chars_of(self.messages, schemas)

    def _chat(self, schemas: list[dict[str, Any]] | None, think: bool | None = ...) -> LLMResponse:
        if think is ...:   # 입력창의 추론 강도(로컬 모델) > 용도 설정
            think = self.settings.think if self.settings.think is not None else self.profile.think
        for attempt in range(4):
            self._fit(schemas)
            try:
                resp = self.llm.chat(self.messages, schemas, think=think)
            except BadRequestError as e:
                if "context" not in str(e).lower() or attempt == 3:
                    raise
                self._tpc *= 1.3   # 어림이 작았다 → 더 보수적으로 잡고 더 줄여서 다시
                continue
            if resp.prompt_tokens and self._sent_chars:   # 실제 토큰 수로 어림을 고친다 (+8% 여유)
                self._tpc = max(0.2, resp.prompt_tokens / self._sent_chars * 1.08)
            return resp
        raise RuntimeError("unreachable")

    def _execute(self, step: int, call: ToolCallRequest) -> Iterator[Event]:
        """도구 하나를 실행한다. 이벤트를 yield 하고, (성공 여부, 모델에게 줄 출력)을 return 한다."""
        tool = self.tools.get(call.name)
        yield ToolCall(step, call.id, call.name, call.arguments, tool.permission if tool else "unknown")

        if tool is None:
            return False, self._msg("unknown_tool", name=call.name, tools=", ".join(self.tools))
        if call.error or (broken := leaked_call_syntax(call.arguments)):
            return False, self._refused("bad_json", self._msg("bad_json", error=call.error or broken))
        if (leaked := leaked_note(call.arguments)) is not None:   # D6: 기록 압축 표시를 내용으로 베껴 씀
            return False, self._refused("compaction_leak", self._msg("compaction_leak", arg=leaked))

        # 반복 감지. 쓰기(파일 저장 등)는 인자만으로 센다. 읽기·실행은 '그 사이에 다른 쓰기·실행이 없었는데 또' 일 때만 센다
        # (D5: 셸 명령으로 파일을 고친 뒤 같은 테스트를 다시 돌리는 것은 반복이 아니다).
        key = f"{tool.name}|{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}"
        if tool.permission in CHANGING or self._after.get(key) == self._version:
            repeats = self._seen[key] = self._seen.get(key, 0) + 1
        else:
            repeats = self._seen[key] = 1
        if repeats >= 3:
            self._loop = True
            return False, self._msg("repeat_blocked")

        if tool.permission == "admin" and not accounts.is_admin(accounts.current()):   # 로그인 사용: 전체 설정을 바꾸는 도구
            text = ("이 일(사내 사이트·API 등록, 내부 주소 허용)은 모두가 같이 쓰는 설정을 바꾸므로 관리자만 할 수 있습니다. "
                    "직접 하지 말고, 사용자에게 관리자에게 요청하라고 안내하세요.")
            yield Note(step, "admin", text.split(".")[0], "")
            return False, self._refused(f"admin:{tool.name}", f"차단됨: {text}")
        if (outside := self._outside_block(tool.name, tool.permission, call.arguments)) is not None:   # E1
            yield Note(step, "outside", outside, "")
            return False, self._refused(f"outside:{tool.name}", outside)
        if (blocked := self._directive_block(tool.permission, call.arguments)) is not None:   # S-3·S-5
            yield Note(step, "directive", blocked.splitlines()[0][:160], "")
            return False, self._refused(f"directive:{tool.name}", blocked)
        if (gate := self._location_gate(tool.name, tool.permission, call.arguments)) is not None:   # 공유 위치 (A2·A6·A7)
            kind, text, scope = gate
            if kind == "refuse":
                yield Note(step, "share", text.splitlines()[0][:160], "")
                return False, self._refused(f"share:{tool.name}", f"차단됨: {text}")
            request = ApprovalRequested(step, call.id, call.name, call.arguments, text, always_ask=True)
            yield request
            approved = bool(self.approver(request))
            yield ApprovalResolved(call.id, approved)
            if not approved:
                return False, (f"사용자가 이 위치를 허용하지 않았습니다: {scope.removeprefix('host:')}. 이 위치는 읽지 않습니다 — "
                               "읽지 못한 내용을 짐작해 쓰지 말고, 읽지 못했다고 답하세요.")
            self._allow_location(scope)
            yield Note(step, "share", f"위치 허용 (읽기 전용): {scope.removeprefix('host:')}", "")

        danger = destructive_reason(tool.name, call.arguments, self.workdir or self.sandbox.cwd)   # D7
        reason = danger or approval_reason(tool, self.profile, self.tainted)
        if danger:
            yield Note(step, "danger", danger, "")
        if reason:
            request = ApprovalRequested(step, call.id, call.name, call.arguments, reason, always_ask=bool(danger))
            yield request
            approved = bool(self.approver(request))
            yield ApprovalResolved(call.id, approved)
            if not approved:
                return False, self._msg("denied")

        try:
            output = tool.call(self.ctx, call.arguments)
            ok = not getattr(output, "failed", False)   # S-9: 거절·빈 응답은 출력은 그대로, 기록은 실패로
            output = str(output)
        except ValidationError as e:
            params = json.dumps(tool.schema()["function"]["parameters"], ensure_ascii=False)
            problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            output, ok = self._refused(f"bad_args:{tool.name}", self._msg("bad_args", problems=problems, schema=params)), False
        except SandboxError as e:
            output, ok = self._refused(_reason_key(tool.name, str(e)), f"차단됨: {e}"), False
        except ToolError as e:
            output, ok = self._refused(_reason_key(tool.name, str(e)), f"오류: {e}"), False
        except Exception as e:  # 도구 버그가 루프 전체를 죽이지 않게 한다
            output, ok = f"오류: {type(e).__name__}: {e}", False

        if tool.taints or (ok and self._reads_share(tool.name, call.arguments)):   # 공유 파일 안의 지시는 데이터 (T9)
            self.tainted = True
        if ok and (tool.permission in CHANGING or tool.permission == "exec"):
            self._version += 1   # 실행 명령도 파일·설치 상태를 바꿀 수 있다
        if tool.permission not in CHANGING:
            self._after[key] = self._version   # 이 호출 자신의 변화는 다음 같은 호출의 '변화'로 치지 않는다
        self._ledger.append(evidence.Step(tool.name, dict(call.arguments), ok, output))
        full = output   # 자르기 전 원문 (지시 파일 고정에 쓴다)
        self._remember(tool.name, tool.permission, call, ok, full)
        if tool.permission == "exec":
            self.known_errors.learn(output)   # E6: '없는 이름' 오류를 기억한다
        save_to = self.ctx.run_dir / f"{call.id}.txt"
        output = truncate_output(output, self.settings.limits.tool_output_chars, save_to, self._can_read(save_to))
        if self.profile.standards and ok:
            output = yield from self._apply_standards(step, tool.name, call.arguments, output)
        output = yield from self._watch_failures(step, tool.name, ok, output)
        for note_kind, text in self._after_call(tool.name, tool.permission, call.arguments, ok):
            yield Note(step, note_kind, text.splitlines()[0][:160], "")
            output += "\n\n" + text
        if ok and tool.permission == "read" and (pinned := self._pin_if_given(call.arguments, full)):   # S-2
            yield Note(step, "pinned", pinned, "")
            output += "\n\n" + self._msg("pinned_note")
        return ok, output + ("\n\n" + self._msg("repeat_warning") if repeats == 2 else "")

    # ───────────────────────── 공유 위치 (기능 '공유 경로·파일 탐색': A2 처음 보는 위치는 확인 · A6 실패 갈래 · A7 이 PC 기준)

    def _location_gate(self, name: str, permission: str, args: dict[str, Any]) -> tuple[str, str, str] | None:
        """('refuse', 이유, '') · ('ask', 승인 창 글, 허용할 범위) · None(평소대로). 파일 도구·fetch_url 의 절대 위치만 본다."""
        raw = args.get("url") if name == "fetch_url" else args.get("path")
        if not isinstance(raw, str) or name not in _LOCATION_TOOLS:
            return None
        if name == "fetch_url" and re.match(r"https?://", raw.strip(), re.I):
            return self._host_gate(raw.strip())
        p = locations.to_path(raw)
        if not locations.is_location(p):
            return None
        self._touched.append(_loc_key(p))
        write = permission == "write"
        try:
            self.sandbox.resolve(p, write=write)
            return None
        except SandboxError as e:
            msg = str(e)
        share_like = locations.is_unc(p) or locations._is_remote_drive(p) or locations.find(p) is not None
        if write:   # 결정 ①: 공유 위치에는 쓰지 않는다
            return ("refuse", "공유 위치에는 쓰지 않습니다 (읽기 전용) — 결과는 작업 폴더에 저장하세요", "") if share_like else None
        if "허용된 폴더 밖" not in msg:
            return "refuse", msg, ""
        if why := locations.refuse_reason(p, self.settings):
            return "refuse", f"{why} ({raw})", ""
        kind, err = locations.probe(p)
        if kind == "error":
            return "refuse", err, ""
        scope = locations.scope_for(p, kind)
        if why := locations.refuse_reason(scope, self.settings):
            return "refuse", f"{why} — '{scope}'", ""
        return ("ask", f"처음 보는 위치를 읽으려 합니다: '{scope}' (이 폴더와 그 아래 · 읽기 전용). 허용할까요? 경로는 에이전트가 도는 이 PC "
                       "기준이고, 허용한 위치는 '상태 · 연결' › 공유 위치에서 보고 지울 수 있습니다.", scope)

    def _host_gate(self, url: str) -> tuple[str, str, str] | None:
        """사내 http(s) 공유 링크: 아직 허용 안 된 내부망 주소면 처음 한 번 사용자에게 묻는다 (A1·A2)."""
        from agent.tools.web import check_url
        try:
            check_url(self.settings, url)
            return None
        except ToolError as e:
            if "내부망 주소라 차단" not in str(e):
                return None   # 주소 오류 등은 도구가 알린다
        host = (urlsplit(url).hostname or "").lower()
        return ("ask", f"처음 보는 사내 주소를 읽으려 합니다: '{host}'. 이 주소의 페이지·파일을 읽도록 허용할까요? (읽기만 · '상태 · 연결'에서 지울 수 "
                       "있습니다)", f"host:{host}")

    def _allow_location(self, scope: str) -> None:
        if scope.startswith("host:"):
            connections.allow_host(self.settings, scope.removeprefix("host:"))
            return
        locations.add(scope, self.settings)
        cwd = self.sandbox.cwd
        self.sandbox = Sandbox.from_settings(self.settings)
        self.sandbox.cwd = cwd
        self.ctx.sandbox = self.sandbox

    def _untried_locations(self) -> list[str]:
        """작업에 적힌 절대 위치 중 이번 실행에서 도구에 한 번도 넘기지 않은 것 (그 위치나 그 위·아래 폴더를 열었으면 시도한 것으로 본다)."""
        if not {"read_file", "list_dir"} & set(self.tools):
            return []
        out = []
        for raw in locations.find_in_text(self._task):
            key = _loc_key(raw)
            if not any(locations.within(key, k) or locations.within(k, key) for k in self._touched) and raw not in out:
                out.append(raw)
        return out

    def _reads_share(self, name: str, args: dict[str, Any]) -> bool:
        raw = args.get("url") if name == "fetch_url" else args.get("path")
        return name in _LOCATION_TOOLS and isinstance(raw, str) and locations.find(locations.to_path(raw)) is not None

    # ───────────────────────── S-7·S-8·S-9: 받은 결과만 근거로

    def _remember(self, name: str, permission: str, call: ToolCallRequest, ok: bool, full: str) -> None:
        """도구 결과의 한 줄 요약을 남기고, 성공한 결과는 '본 것'에 넣는다. 이번 실행에서 쓴 파일을 다시 읽은 것은
        넣지 않는다 (QA S03: 지어낸 값으로 쓴 보고서를 다시 읽고 그 값을 근거처럼 썼다)."""
        self._digests[call.id] = grounding.digest(name, call.arguments, ok, full)
        target = str(call.arguments.get("path") or call.arguments.get("url") or "")
        resolved = ""
        if target and not re.match(r"https?://", target, re.I):
            try:
                resolved = str(self.sandbox.resolve(target)).lower()
            except SandboxError:
                resolved = ""
        if permission == "write" and ok and resolved:
            self._written.add(resolved)
        elif ok and resolved not in self._written:
            self._corpus.add(full)

    def _documented(self, path: str) -> bool | None:
        """등록된 API 문서에 있는 경로인지 (문서가 짧거나 없으면 모름 = None)."""
        known: bool | None = None
        base = path.rstrip("/") or "/"
        for api in self.settings.apis:
            paths = [m.group(2).split("?")[0] for line in api.docs.splitlines() if (m := openapi.ENDPOINT_LINE.match(line))]
            if len(paths) < 5:
                continue
            known = False
            if any(path_template(p).match(base) for p in paths):
                return True
        return known

    def _grounding(self, answer: str) -> list[str]:
        """사내 API 를 쓴 실행의 최종 답과 이번에 저장한 보고 파일을 실행 기록과 대조한다 (개발 작업은 제외)."""
        if self._grounded and self._grounded[0] == answer:
            return self._grounded[1]
        problems: list[str] = []
        if any(st.name in grounding.API_TOOLS for st in self._ledger) and not (self.profile.standards and self._dev):
            problems = grounding.check(answer, self._ledger, self._corpus, self._documented)
            reports: dict[str, str] = {}
            for st in self._ledger:
                path, content = st.args.get("path"), st.args.get("content")
                if st.ok and st.name == "write_file" and isinstance(path, str) and isinstance(content, str) \
                        and path.lower().endswith(grounding.REPORT_EXT):
                    reports[Path(path).name] = content
            for name, content in reports.items():
                problems += grounding.check(content, self._ledger, self._corpus, self._documented, where=f"보고 파일 '{name}'")
        self._grounded = (answer, problems)
        return problems

    def _step_gaps(self, answer: str) -> tuple[list[int], list[tuple[int, int]]]:
        """지시한 단계 중 답에 결과가 없는 단계, 'N줄 요약' 인데 요약이 없는 단계."""
        d = self._directives
        if not d.steps:
            return [], []
        missing = steps_missing(answer, d.steps)
        return missing, [(n, k) for n, k in d.summary_missing(answer) if n not in missing]   # 빠진 단계면 그쪽으로 알린다

    # ───────────────────────── API-10: 같은 이유의 거부가 이어지면 멈춘다 / API-9: 모델이 읽을 수 있는 곳인지

    def _refused(self, kind: str, text: str) -> str:
        """거부(차단·형식 오류·같은 도구 오류)를 이유별로 센다. 3번째부터 방법을 바꾸라고 하고, REFUSE_STOP 번째면 멈춘다
        (QA A06: 생략 표시 문구로 파일 쓰기를 27번 되풀이 — 이 거부는 반복 감지 앞에서 돌아가 세어지지 않았다)."""
        n = self._refusals[kind] = self._refusals.get(kind, 0) + 1
        if n < 3:
            return text
        if n >= REFUSE_STOP:
            self._loop = True
        return text + "\n\n" + self._msg("refused_again", count=n,
                                         stop=" 다음 차례에 실행을 멈춥니다." if n >= REFUSE_STOP else "")

    # ───────────────────────── S-2·S-3·S-5: 사용자가 준 지시

    def _directive_block(self, permission: str, args: dict[str, Any]) -> str | None:
        d = self._directives
        if permission == "send" and d.read_only:   # S-3: 마지막 방어선(GET 전용 등록)에 기대지 않는다
            return self._msg("read_only_blocked", quote=d.read_only)
        if permission == "write" and isinstance(args.get("path"), str) and self._given:
            try:
                target = str(self.sandbox.resolve(args["path"], write=True))
            except SandboxError:
                return None
            if given := d.confusable(target, self._given):   # S-5: 사용자가 준 파일과 헷갈리는 산출물을 만들지 않는다
                return self._msg("given_file", name=given)
        return None

    def _wrong_paths(self, answer: str) -> list[evidence.Finding]:
        """S-6: 답에 적은 저장 경로가 이번에 실제로 저장한 경로와 다르면 알린다 (잘린 폴더 이름 등)."""
        out = []
        for st in self._ledger:
            if not (st.ok and st.name == "write_file" and isinstance(st.args.get("path"), str)):
                continue
            try:
                actual = self.sandbox.resolve(st.args["path"])
            except SandboxError:
                continue
            for m in re.finditer(rf"[A-Za-z]:[\\/][^\n`'\"|<>]*?{re.escape(actual.name)}", answer):
                if m.group(0).replace("/", "\\").lower() != str(actual).lower():
                    out.append(evidence.Finding(f"답에 적은 저장 경로가 실제와 다릅니다 — 실제 경로: {actual}", False))
                    break
        return out[:2]

    def _given_files(self) -> list[str]:
        """사용자가 가리킨 파일 중 실행 시작 때 이미 있는 것 (사용자가 '준' 자료). 앞으로 만들 산출물 이름은 빠진다."""
        out = []
        for f in self._directives.files:
            try:
                p = self.sandbox.resolve(f)
            except SandboxError:
                continue
            if p.is_file():
                out.append(str(p))
        return out

    def _pin_if_given(self, args: dict[str, Any], output: str) -> str:
        """사용자가 가리킨 지시 파일을 읽었으면 원문을 시스템 지침에 고정한다 (압축돼도 지시가 남게). 고정했으면 설명."""
        target = str(args.get("url") or args.get("path") or "")
        ref = self._directives.refers_to(target) if target else ""
        if not ref:
            return ""
        body = re.sub(r"\A(?:제목:[^\n]*\n)?(?:주소:[^\n]*\n)?\n?", "", output)
        body = re.sub(r"\n*\((?:본문 끝|본문 전체)[^)]*\)\s*\Z", "", body).strip()
        if not self._directives.pin(target, body):
            return ""
        self.messages[0]["content"] = self.system_prompt()
        extra = f" · 금지: '{self._directives.read_only}' → 변경 도구를 이번 실행에서 뺌" if self._directives.read_only else ""
        return f"사용자가 준 지시 파일 원문을 실행 내내 유지 (시스템 지침에 고정){extra}"

    def _handoff(self, answer: str) -> str:
        """답이 사용자에게 스크립트·명령 실행을 넘기는데, 사용자가 못 한다고 했거나 이 용도에 실행 도구가 없으면 그 이유 (QA API-11)."""
        if not _RUN_ASK.search(answer):
            return ""
        said = [m.group(0) for msg in self.messages if msg.get("role") == "user"
                for m in [_USER_CANT.search(str(msg.get("content") or ""))] if m]
        if said:
            return f"사용자가 '{said[-1].strip()}' 라고 했습니다."
        if not any(t.permission == "exec" for t in self.tools.values()):
            return "이 용도에는 코드를 실행할 도구가 없어, 만든 스크립트도 실행·확인되지 않았습니다."
        return ""

    def _can_read(self, path: Path) -> bool:
        """모델이 read_file 로 이 파일을 열 수 있는지 (도구가 있고, 허용 폴더 안)."""
        if "read_file" not in self.tools:
            return False
        try:
            self.sandbox.resolve(str(path))
            return True
        except SandboxError:
            return False

    # ───────────────────────── E1: 작업 폴더 경계 / E6: 고친 오류의 재발

    def _outside_block(self, name: str, permission: str, args: dict[str, Any]) -> str | None:
        if self._boundary is None:
            return None
        if permission == "write" and isinstance(args.get("path"), str):
            try:
                return self._boundary.check_write(self.sandbox.resolve(args["path"], write=True))
            except SandboxError:
                return None   # 샌드박스가 도구에서 이유를 붙여 막는다
        if name in evidence.EXEC_TOOLS:
            return self._boundary.check_command(str(args.get("command") or args.get("code") or ""))
        return None

    def _after_call(self, name: str, permission: str, args: dict[str, Any], ok: bool) -> list[tuple[str, str]]:
        """도구 결과 끝에 붙일 알림 [(Note 종류, 모델에게 줄 말)]."""
        out: list[tuple[str, str]] = []
        if permission == "exec" and self._boundary is not None and (warn := self._boundary.after_command()):
            out.append(("outside", warn))
        text = str(args.get("command") or args.get("code") or "")
        if permission == "exec" and self.profile.standards and not self._v1_hinted and _SYNTAX_ONLY.search(text) \
                and not evidence._V1_NAMES.search(text):   # E11: 문법 검사만 하고 V1 이라 하지 않게
            self._v1_hinted = True
            out.append(("v1", self._msg("v1_hint")))
        if ok and permission == "write" and isinstance(args.get("content"), str) and isinstance(args.get("path"), str):
            try:
                path = self.sandbox.resolve(args["path"])
            except SandboxError:
                return out
            if path.suffix.lower() in codecheck.MEDIA_EXT:   # E7: 글자로 쓴 영상·이미지·문서 파일은 그 형식이 아니다
                out.append(("fake_file", f"⚠ '{path.name}' 은(는) {path.suffix} 이진 형식인데 글자로 썼습니다 — 진짜 {path.suffix} 파일이 "
                                         "아닙니다. 테스트 입력이면 그 테스트는 근거가 아니니 실제 형식의 파일을 쓰세요."))
            hits = self.known_errors.check(path, args["content"], self.workdir or self.sandbox.cwd)
            if hits:
                items = "\n".join(f"- '{b.name}' — 앞서: {b.line[:160]}" for b in hits[:4])
                out.append(("regression", self._msg("regression", items=items)))
        return out

    # ───────────────────────── B2: 언어별 규격 / A6: 같은 종류의 실패

    def _apply_standards(self, step: int, name: str, args: dict[str, Any], output: str) -> Iterator[Event]:
        """개발을 시작한 언어를 알아채면 그 언어 규격 하나만 시스템 프롬프트에 붙인다 (16k 컨텍스트 절약)."""
        lang = args.get("name") if name == "read_standard" and args.get("name") in standards.LANG_LABEL else \
            standards.detect_from_call(name, args)
        if name == "write_file" and lang or (name == "run_command" and re.search(
                r"\b(dotnet\s+new|npm\s+init|python3?\s+-m\s+venv|py\s+-m\s+venv)\b", str(args.get("command", "")), re.I)):
            self._dev = True   # 코드·프로젝트 파일을 만들기 시작했다 → 끝날 때 규격 점검 대상
        if not lang or self._lang:
            if name == "read_standard" and args.get("name") == self._lang:
                return self._msg("lang_already")
            return output
        self._lang = lang
        self.messages[0]["content"] = self.system_prompt()
        label = standards.LANG_LABEL[lang]
        yield Note(step, "standard", f"{label} 개발로 보여 언어별 규격을 지침에 붙임", "")
        if name == "read_standard":
            return self._msg("lang_attached_read", label=label)
        return output + "\n\n" + self._msg("lang_attached", label=label)

    def _watch_failures(self, step: int, name: str, ok: bool, output: str) -> Iterator[Event]:
        """같은 종류의 실패(시간 초과, 빌드 실패, 같은 예외…)가 최근 실행에 몰리면 방법을 바꾸라고 개입한다."""
        if name not in ("run_command", "run_python"):
            return output
        kind = failure_kind(name, ok, output)
        self._fails.append(kind)
        if kind is None:
            return output
        count = sum(1 for k in self._fails if k == kind)
        if count not in FAIL_WARN_AT:
            return output
        guide = self._msg(FAIL_GUIDE.get(kind, "fail_guide_other"))
        yield Note(step, "failures", f"같은 종류의 실패({kind})가 최근 {len(self._fails)}번 실행 중 {count}번이라 경고함", "")
        strong = " " + self._msg("fail_strong") if count >= FAIL_WARN_AT[-1] else ""
        return output + "\n\n" + self._msg("fail_warning", kind=kind, window=len(self._fails), count=count,
                                           guide=guide, strong=strong)


def profile_sampling(base: Sampling, profile: Profile) -> Sampling:
    """프로필에 온도·top_p 를 적었으면 그 값으로 (용도별 파라미터)."""
    overrides = {k: getattr(profile, k) for k in ("temperature", "top_p") if getattr(profile, k) is not None}
    return base.model_copy(update=overrides) if overrides else base


# API-11: 사용자가 밝힌 제약('파이썬 실행 못 해')과, 답이 사용자에게 실행을 넘기는 말
_USER_CANT = re.compile(r"(파이썬|python|스크립트|코드|명령(?:어|창)?|터미널|cmd|powershell|프로그래밍|개발)[^.\n]{0,20}?"
                        r"(못\s*해|못\s*합|못\s*하|할\s*줄\s*몰|할\s*줄\s*모르|모르|안\s*돼|불가|어려워)", re.I)
_RUN_ASK = re.compile(r"(실행하(?:시면|세요|십시오|면 됩니다)|실행해\s*(?:보세요|주세요|보시면|주시면)|돌려\s*(?:보세요|주세요|보시면)|"
                      r"\b(?:run|execute)\s+(?:the\s+|this\s+)?(?:script|command|file|program)\b|python\s+\S+\.py)", re.I)
_LOCATION_TOOLS = {"list_dir", "read_file", "search_files", "fetch_url", "write_file"}


def _loc_key(raw: str) -> str:
    """위치 비교용 (realpath 를 쓰지 않는다 — 연결 안 되는 서버에서 멈추지 않게)."""
    return os.path.normcase(os.path.normpath(locations.to_path(raw)))


# API-13: 요청한 것 중 답하지 못했다는 말 (확인·조회·계산을 못 함, 알 수 없음, 스크립트를 돌려야 알 수 있음)

# 이미지를 보여 달라는 요청 (기능 '이미지 표시' · QA IMG-2·DONE-1). '가져온다'·'개수' 같은 말은 아니다
IMAGE_ASK = re.compile(
    r"(이미지|사진|그림|썸네일|image|picture|photo|thumbnail)[^\n]{0,40}?(보여|띄워|표시해|볼\s*수\s*있게|열어\s*줘|\bshow\b|\bdisplay\b)"
    r"|(보여|띄워|\bshow\b|\bdisplay\b)[^\n]{0,30}?(이미지|사진|그림|썸네일|image|picture|photo)"
    r"|\.(?:bmp|png|jpe?g|gif|tiff?|webp)\b[^\n]{0,60}?(보여|띄워|\bshow\b)", re.I)
# 도구를 쓰지 않고 '못 한다 · 알려 주시면' 으로 넘기는 답 (QA LONG-1: 도구 0회로 'UUID 를 알려 주시면' 9천 토큰)
_ASK_BACK = re.compile(r"(알려|제공해|확인해|말씀해)\s*주시(?:면|겠어요|겠습니까|기\s*바랍니다)|보여\s*드릴\s*수\s*없|조회할\s*수\s*없|"
                       r"(?:찾을|가져올|확인할)\s*수\s*(?:가\s*)?없", re.I)

_UNANSWERED = re.compile(
    r"(?:확인|조회|계산|집계|파악|답변|제공)(?:을|를)?\s*(?:하|할|해|해\s*드릴|해드릴|해\s*드리|해드리)?\s*(?:지\s*)?"
    r"(?:못\s*했|못\s*하였|못\s*합|못\s*해|수\s*(?:가\s*)?없)"
    r"|알\s*수\s*(?:가\s*)?없(?:습니다|어요|었습니다|음)"
    r"|(?:되면|되시면|하시면|넣으시면|넣으면|입력하시면|후에|뒤에)[^.\n]{0,40}(?:확인|조회|알려\s*드릴|답해\s*드릴)(?:하실|할|해\s*드릴)?\s*수\s*있"
    r"|(?:스크립트|코드|명령|\S+\.(?:py|js|ps1|sh|bat))[^.\n]{0,25}실행(?:하시면|해\s*주시면|해야)"
    r"|\b(?:could not|couldn't|unable to|was not able to|cannot|can't)\s+(?:determine|retrieve|confirm|calculate|count|answer)\b",
    re.I)


def unanswered_part(answer: str) -> str:
    """답 안에서 '못 했다·알 수 없다' 를 말한 문장 (없으면 빈 글자). 코드·인용 안은 보지 않는다."""
    plain = re.sub(r"```.*?```|`[^`\n]*`", " ", answer, flags=re.S)
    for s in re.split(r"(?<=[.!?。])\s+|\n", plain):
        if _UNANSWERED.search(s):
            return s.strip(" -*#>")
    return ""


def _reason_key(tool_name: str, message: str) -> str:
    """도구 오류를 '같은 이유'로 묶는 키: 경로·숫자·따옴표 안 값은 뺀 첫 줄 앞부분."""
    first = message.strip().splitlines()[0] if message.strip() else ""
    first = re.sub(r"'[^']*'|\"[^\"]*\"|[A-Za-z]:[\\/]\S*|https?://\S+|\d+", "_", first)
    return f"{tool_name}:{first[:50]}"


def looks_like_plan(text: str) -> bool:
    """'확인하고 설치한 후 작성하겠습니다' 처럼 할 일을 예고만 하는 답인지. 질문·조건부 제안("원하시면 …하겠습니다")은 아니다."""
    t = text.strip()
    if not t or _QUESTION_END.search(t):
        return False
    sentences = [s.strip() for s in re.split(r"(?<=[.!。\n])\s*", t) if s.strip()]
    return any(_FUTURE.search(s) and _ACTION.search(s) and not _CONDITIONAL.search(s) for s in sentences[-2:])


# E2: 끊긴 답. 모델이 문장 도중에 도구 호출을 시작했다 깨지면 서버가 그 호출을 버려 '이제 V2 단' 같은 글만 온다 (QA R15).
_TERMINAL = re.compile(r"([.!?。…)\]}>»」』\"'`*|:~]|[다요죠까오음함됨임네라지게]|```)\s*$")
_PROGRESS = re.compile(r"(이제|다음으로|다음은|먼저|그럼|그러면|이어서|\b(?:now|next|then|let me|first)\b)", re.I)


_SYNTAX_ONLY = re.compile(r"\bcompileall\b|\bpy_compile\b|node\s+--check", re.I)   # V1 ① 만 보는 명령
# 자격 증명 단어 바로 뒤(가까이)에 '알려·보내 주세요' 가 붙은 요청만 본다 — '비밀번호 재시도 … 말씀해 주세요' 같은 문장은 아니다
_CRED = r"(?:비밀\s*번호|비번|패스워드|password|passwd|토큰|token|api\s*key|api\s*키|자격\s*증명|credentials?|계정\s*정보|로그인\s*정보)"
_ASK_KO = re.compile(_CRED + r"[)\s]*(?:\([^)]{0,20}\))?\s*(?:[을를와과이가]|,|/|및|나|이나)?[^.!?\n]{0,12}?"
                     r"(?:알려|보내|입력해|적어|말씀해|공유해|전달해)\s*(?:주|줄)", re.I)
_ASK_EN = re.compile(r"\b(?:send|provide|share|give|tell|enter)\s+(?:me\s+|us\s+)?(?:your\s+|the\s+)?"
                     r"(?:username|password|token|credentials?|api\s*key)", re.I)
_UI_PLACE = re.compile(r"(관리\s*화면|설정\s*화면|상태\s*·\s*연결|인증\s*칸|management\s+(?:screen|page|ui))", re.I)


def asks_for_credentials(text: str) -> bool:
    """답이 사용자에게 비밀번호·토큰을 채팅으로 달라고 하는지 (관리 화면에 넣으라는 안내, 코드·인용 속 글자는 아니다)."""
    plain = re.sub(r"```.*?```|`[^`\n]*`|'[^'\n]*'|\"[^\"\n]*\"", " ", text, flags=re.S)
    for s in re.split(r"(?<=[.!?。])\s+|\n", plain):
        if (_ASK_KO.search(s) or _ASK_EN.search(s)) and not _UI_PLACE.search(s):
            return True
    return False


# S-3: 변경을 금지한 작업에서 '변경 요청 허용을 켜 달라'는 답 / S-4: 할 일을 예고하며 끝난 답
_UNLOCK = re.compile(r"(변경\s*(?:요청\s*)?허용|쓰기\s*허용|write\s*access|allow\s*(?:changes|writes?))[^.\n]{0,40}"
                     r"(켜|활성|허용해|enable|turn\s*on)", re.I)
_PLAN_END = re.compile(r"(하겠습니다|할게요|드리겠습니다|진행합니다|시작합니다|하도록 하겠습니다)[.!…]*\s*$")


def ends_with_plan(answer: str) -> bool:
    """답의 마지막 문장이 '이제 최종 답변을 하겠습니다' 처럼 할 일을 예고하는지 (조건부 제안 '필요하면 … 드리겠습니다' 는 아님)."""
    plain = re.sub(r"```.*?```", " ", answer, flags=re.S).strip()
    sentences = [s.strip(" *#>-") for s in re.split(r"(?<=[.!?。])\s+|\n", plain) if s.strip(" *#>-")]
    last = sentences[-1] if sentences else ""
    return bool(_PLAN_END.search(last)) and not _CONDITIONAL.search(last + " ") and not _QUESTION_END.search(last)


def looks_unfinished(text: str, finish_reason: str = "") -> bool:
    """길이 한도로 잘렸거나(length), 도구 호출이 깨졌거나(tool_calls 인데 호출 없음), 짧은 답이 '이제 …' 문장 중간에서 끝났는지."""
    if finish_reason in ("length", "tool_calls"):
        return True
    t = text.strip()
    if not t or len(t) > 300 or _TERMINAL.search(t):
        return False
    return bool(_PROGRESS.search(re.split(r"(?<=[.!?。])\s+|\n", t)[-1]))


def history_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """저장된 이벤트로 대화 기록을 다시 만든다 ('이어서 하기'). 시스템 프롬프트는 새로 만들므로 넣지 않는다."""
    if any(ev.get("type") == "WorkflowStarted" for ev in events):
        # 워크플로 실행은 노드마다 다른 에이전트의 대화라 그대로 이을 수 없다 → 요청과 최종 결과만 넘긴다
        start = next((ev for ev in events if ev.get("type") == "RunStarted"), None)
        final = next((ev for ev in reversed(events) if ev.get("type") == "Final"), None)
        return [m for m in ({"role": "user", "content": start["task"]} if start else None,
                            {"role": "assistant", "content": final["answer"]} if final else None) if m]
    msgs: list[dict[str, Any]] = []
    answered: set[str] = set()
    for ev in events:
        t = ev.get("type")
        if t == "RunStarted":
            msgs.append({"role": "user", "content": ev["task"]})
        elif t == "LLMCall":
            msg: dict[str, Any] = {"role": "assistant", "content": ev["content"]}
            if ev["tool_calls"]:
                msg["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                    "name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                    for c in ev["tool_calls"]]
            msgs.append(msg)
        elif t == "ToolResult":
            msgs.append({"role": "tool", "tool_call_id": ev["call_id"], "content": ev["output"]})
            answered.add(ev["call_id"])
        elif t == "Note" and ev.get("injected"):
            msgs.append({"role": "user", "content": ev["injected"]})   # 루프가 되돌려 보낼 때 넣은 말
        elif t == "Final" and ev["status"] in ("max_steps", "timeout", "loop", "cancelled"):
            msgs.append({"role": "assistant", "content": ev["answer"]})   # 한도·반복으로 멈췄을 때의 정리 답변
    # 중지 등으로 결과가 없는 도구 호출에는 빈 결과를 채운다 (API 형식상 호출마다 결과가 있어야 한다)
    out: list[dict[str, Any]] = []
    for msg in msgs:
        out.append(msg)
        for c in msg.get("tool_calls", []):
            if c["id"] not in answered:
                out.append({"role": "tool", "tool_call_id": c["id"], "content": "(실행되지 않음)"})
    return out
