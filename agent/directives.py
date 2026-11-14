"""사용자가 따르라고 준 지시 — 지시 파일 원문 고정, '조회만' 같은 금지, 번호 매긴 단계 (QA S-2·S-3·S-4·S-5).

긴 실행에서 컨텍스트를 줄이면 오래된 도구 결과가 잘린다. 지시 파일 내용이 도구 결과로만 있으면 그때 사라지고,
소형 모델은 기억으로 지시를 지어내 따른다 (QA S02: '조회만' 지시서를 '이미지 첨부' 지시서로 바꿔 써서 따름).
그래서 사용자가 가리킨 지시 파일을 읽으면 원문을 시스템 지침에 고정하고(압축되지 않음), 사용자 말과 그 원문에서
'조회만'·'바꾸지 말 것' 같은 금지를 찾아 변경 도구를 이번 실행에서 뺀다. 과제·API 와 무관한 일반 규칙만 둔다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

PIN_MAX = 3000          # 시스템 지침에 고정할 원문 최대 글자 (16k 컨텍스트 보호)
STEP_MIN = 3            # 이만큼 이상 번호 매긴 단계가 있어야 단계 점검을 한다

_FILE_EXT = r"(?:md|txt|html?|json|ya?ml|csv|tsv|xml|ini|toml|docx?|xlsx?|pdf)"
_ABS_PATH = re.compile(rf"[A-Za-z]:[\\/][^\n\"'<>|*?]*?\.{_FILE_EXT}\b", re.I)
_NAME = re.compile(rf"[\w가-힣.\-()]+\.{_FILE_EXT}\b", re.I)
_INSTRUCTION_WORDS = re.compile(r"(지시|순서|절차|단계|차례|따라|지침|매뉴얼|instruction|procedure|step)", re.I)
_STEP = re.compile(r"^\s{0,3}(?:\*\*)?(\d{1,2})\s*[.)](?:\*\*)?\s+\S", re.M)
_READ_ONLY = re.compile(
    r"(조회만|읽기만|보기만|확인만\s*(?:하|해)|GET\s*만|"
    r"(?:아무것도|아무 것도|무엇도|데이터(?:를|는)?|원본(?:을|은)?)\s*(?:바꾸|변경하|수정하|삭제하|지우|건드리|쓰)지\s*(?:말|마|않)|"
    r"(?:변경|수정|삭제|쓰기)\s*(?:은|는)?\s*(?:금지|하지\s*(?:말|마)|불가)|"
    r"\bread[- ]?only\b|\b(?:do not|don'?t|never)\s+(?:change|modify|write|delete|update)\b)", re.I)


def referenced_files(texts: list[str]) -> list[str]:
    """사용자 말에 적힌 파일 (전체 경로 또는 파일 이름)."""
    out: list[str] = []
    for t in texts:
        out += [m.group(0).strip() for m in _ABS_PATH.finditer(t)]
        out += [m.group(0) for m in _NAME.finditer(t)]
    return list(dict.fromkeys(out))


def _norm(p: str) -> str:
    return p.strip().strip('"\'').replace("/", "\\").lower()


def matches(ref: str, target: str) -> bool:
    """도구 인자(경로·file://)가 사용자가 가리킨 파일인지 — 전체 경로가 같거나 끝이 같거나 이름이 같다."""
    a, b = _norm(ref), _norm(re.sub(r"^file:/*", "", target, flags=re.I))
    return bool(a and b) and (a == b or b.endswith(a) or a.endswith(b) or
                              PureWindowsPath(a).name == PureWindowsPath(b).name)


def read_only_quote(text: str) -> str:
    m = _READ_ONLY.search(text)
    if not m:
        return ""
    line = text[text.rfind("\n", 0, m.start()) + 1: text.find("\n", m.end()) if "\n" in text[m.end():] else len(text)]
    return re.sub(r"[*#>`]+", "", line).strip()[:120]


def numbered_steps(text: str) -> list[int]:
    """맨 앞 수준의 번호 매긴 단계 (1. 2. 3. …). 들여쓴 하위 항목(- …)은 세지 않는다."""
    nums = [int(n) for n in _STEP.findall(text)]
    seen: list[int] = []
    for n in nums:
        if n not in seen:
            seen.append(n)
    return seen if len(seen) >= STEP_MIN and seen[:2] == [1, 2] else []


def step_texts(text: str) -> dict[int, str]:
    """번호 매긴 단계 → 그 줄의 글 (처음 나온 것)."""
    out: dict[int, str] = {}
    for m in re.finditer(r"^\s{0,3}(?:\*\*)?(\d{1,2})\s*[.)](?:\*\*)?\s+(\S[^\n]*)", text, re.M):
        out.setdefault(int(m.group(1)), m.group(2).strip())
    return out


_KO_NUM = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7}
_N_LINES = re.compile(r"(\d{1,2}|한|두|세|네|다섯|여섯|일곱)\s*(?:줄|문장|줄짜리)\s*(?:로|으로|안팎으로)?\s*(?:요약|정리)|"
                      r"\bsummar\w*\s+in\s+(\d{1,2})\s+(?:lines|sentences)\b", re.I)
_SUMMARY_WORD = re.compile(r"요약|summary", re.I)
_SUMMARY_CLAIM = re.compile(r"요약\s*(?:을|를)?\s*(?:제공|했|함|완료|드림|드립니다|합니다|하였)", re.I)
_BLOCK_END = re.compile(r"^\s*(?:#{1,6}\s|---|\*\*\*|___|\|)")


def summary_lines(step: str) -> int:
    """'세 줄로 요약' 처럼 요약 줄 수를 정한 단계면 그 줄 수, 아니면 0."""
    m = _N_LINES.search(step or "")
    if not m:
        return 0
    word = m.group(1) or m.group(2)
    return _KO_NUM.get(word) or int(word)


def summary_present(answer: str, need: int) -> bool:
    """답에 '요약' 제목·표시 뒤로 need 줄(또는 문장) 이상의 내용이 있는지. '요약 제공' 같은 주장 줄은 세지 않는다 (QA S-11)."""
    lines = answer.splitlines()
    for i, line in enumerate(lines):
        if not _SUMMARY_WORD.search(line):
            continue
        count = 0
        rest = re.sub(r"^.*?(?:요약|summary)\w*\s*[:：]\s*", "", line, flags=re.I) if re.search(r"(요약|summary)\w*\s*[:：]\s*\S", line, re.I) else ""
        for text in ([rest] if rest else []) + lines[i + 1:]:
            s = text.strip()
            if not s:
                if count:
                    break
                continue
            if _BLOCK_END.match(s) or _SUMMARY_CLAIM.search(s):
                break
            count += max(1, len(re.findall(r"[.!?。](?:\s|$)|다\.|요\.", s)))
        if count >= need:
            return True
    return False


def steps_missing(answer: str, steps: list[int]) -> list[int]:
    """답에서 단계 번호로 결과를 적지 않은 단계 ('3단계', '3.', '3)', '[3]', 'step 3', 표의 첫 칸 3)."""
    missing = []
    for n in steps:
        pat = rf"(?<!\d){n}\s*단계|(?:^|\n)\s*[#*>\-|]*\s*\**{n}\s*[.)\]:]|\[{n}\]|\bstep\s*{n}\b|\|\s*{n}\s*\|"
        if not re.search(pat, answer, re.I):
            missing.append(n)
    return missing


@dataclass
class Directives:
    files: list[str] = field(default_factory=list)            # 사용자가 가리킨 파일
    pinned: list[tuple[str, str]] = field(default_factory=list)   # (어디서, 원문)
    read_only: str = ""                                       # 금지 문구 (있으면 변경 도구를 뺀다)
    steps: list[int] = field(default_factory=list)            # 번호 매긴 단계
    wants_order: bool = False                                 # 사용자가 순서·지시를 따르라고 했는지
    texts: dict[int, str] = field(default_factory=dict)       # 단계 번호 → 그 단계의 글 (요약 줄 수 점검에 쓴다)

    @classmethod
    def from_user(cls, texts: list[str]) -> Directives:
        joined = "\n".join(texts)
        d = cls(files=referenced_files(texts), read_only=read_only_quote(joined),
                wants_order=bool(_INSTRUCTION_WORDS.search(joined)))
        if d.wants_order:
            d.steps = numbered_steps(texts[-1] if texts else "")
            if d.steps:
                d.texts = step_texts(texts[-1])
        return d

    def summary_missing(self, answer: str) -> list[tuple[int, int]]:
        """'N줄로 요약' 단계인데 답에 그 요약이 없으면 [(단계, 줄 수)] (QA S-11: '요약 제공'이라고만 씀)."""
        out = []
        for n in self.steps:
            need = summary_lines(self.texts.get(n, ""))
            if need and not summary_present(answer, need):
                out.append((n, need))
        return out

    def refers_to(self, target: str) -> str:
        return next((f for f in self.files if matches(f, target)), "")

    def pin(self, source: str, content: str) -> bool:
        """지시로 보이는 원문을 고정한다 (사용자가 순서·지시를 따르라고 했거나 번호 매긴 단계가 있는 파일). 새로 고정하면 True."""
        if any(s == source for s, _ in self.pinned):
            return False
        steps = numbered_steps(content)
        if not (self.wants_order or steps):
            return False
        used = sum(len(c) for _, c in self.pinned)
        room = PIN_MAX - used
        if room < 200:
            return False
        text = content if len(content) <= room else content[:room] + f"\n…(이하 {len(content) - room}자 생략 — 원문은 {source})"
        self.pinned.append((source, text))
        self.read_only = self.read_only or read_only_quote(content)
        if not self.steps and steps:
            self.steps, self.texts = steps, step_texts(content)
        return True

    def confusable(self, target: str, given: list[str]) -> str:
        """쓰려는 파일(실제 경로)이 사용자가 준 파일(실행 시작 때 이미 있던 것)과 헷갈리면 그 원본 경로.
        - 같은 이름인데 다른 곳에 새로 만드는 것 (사본·지어낸 지시서)
        - 고정한 지시 파일 자체를 덮어쓰는 것
        같은 파일을 고치는 것(예: 'README.md 고쳐줘')은 막지 않는다."""
        t = _norm(target)
        pinned = {_norm(s) for s, _ in self.pinned}
        for g in given:
            gn = _norm(g)
            if PureWindowsPath(gn).name != PureWindowsPath(t).name:
                continue
            if gn != t or gn in pinned:
                return g
        return ""
