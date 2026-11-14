"""보고의 근거 대조 — 이번 실행에서 실제로 받은 것만 보고에 쓰게 한다 (QA S-7·S-8·S-9·S-11).

QA S03 에서 소형 모델은
  - 컨텍스트를 줄이며 지운 앞쪽 결과(리뷰 현황·프로젝트 이름)를 기억으로 지어내 보고했고,
  - 잘려서 안 보이는 목록 뒤쪽의 파일명을 끝자리만 바꿔 만들어 조회했고,
  - 부른 적 없는 경로를 근거로 적었다.
여기서는 특정 API·지시서와 무관한 규칙만 쓴다.

- Seen   : 이번 실행에서 실제로 본 글 (사용자 말·시스템 지침·성공한 도구 결과·API 응답 전체).
           식별자(파일명·ID)가 여기 없으면 지어낸 것이다.
- digest : 도구 결과 한 줄 요약. 오래된 기록을 지울 때 남기고(S-7), 근거 대조로 되돌려 보낼 때 보여 준다.
- check  : 최종 답·보고 파일에 적은 경로·식별자를 실행 기록과 대조한다.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

API_TOOLS = ("api_get", "api_send")
REPORT_EXT = (".md", ".txt", ".csv", ".tsv", ".html", ".htm")
LINE = 420            # digest 한 줄 최대 글자
LINE_LIST = 640       # 긴 목록의 digest (앞쪽 항목 이름을 순서대로 남긴다 — '처음 N개' 작업이 순서를 잃지 않게)
SUMMARY_MAX = 4000    # 되돌려 보낼 때 붙이는 '실제로 받은 결과' 최대 글자
SEEN_MAX = 8_000_000  # 이번 실행에서 기억할 글자 (넘으면 오래된 것부터 버린다)


class Seen:
    """이번 실행에서 실제로 본 글. 대소문자를 가리지 않고 찾는다."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0

    def add(self, text: str) -> None:
        if not text:
            return
        low = str(text).lower()
        self.parts.append(low)
        self.size += len(low)
        while self.size > SEEN_MAX and len(self.parts) > 1:
            self.size -= len(self.parts.pop(0))

    def has(self, token: str) -> bool:
        t = token.lower()
        return any(t in p for p in self.parts)


def seen_of(memo: dict[str, Any]) -> Seen | None:
    """실행 중이면 그 실행의 Seen (도구를 실행 밖에서 직접 부르면 None — 대조하지 않는다)."""
    s = memo.get("seen")
    return s if isinstance(s, Seen) else None


# ───────────────────────── 식별자

_TOKEN = re.compile(r"(?<![A-Za-z0-9_.+\-])[A-Za-z0-9][A-Za-z0-9_.+\-]{6,}[A-Za-z0-9](?![A-Za-z0-9_+\-])")
_UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$|^[0-9a-f]{16,}$", re.I)
_EXT = re.compile(r"\.[A-Za-z][A-Za-z0-9]{1,4}$")


def is_identifier(token: str) -> bool:
    """지어내면 안 되는 값처럼 생긴 글자: UUID·긴 16진수, 숫자가 섞인 파일 이름, 숫자가 많이 섞인 긴 코드.
    낱말·버전(qwen3.5, utf-8)·날짜는 아니다."""
    t = token.strip()
    if len(t) < 8:
        return False
    if _UUID.match(t):
        return any(c.isdigit() for c in t)
    digits = sum(c.isdigit() for c in t)
    letters = sum(c.isalpha() for c in t)
    if _EXT.search(t) and digits >= 3 and letters >= 2:
        return True
    return len(t) >= 10 and digits >= 4 and letters >= 2


def identifiers(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in _TOKEN.finditer(text or "") if is_identifier(m.group(0))))


def path_identifiers(path: str) -> list[str]:
    """API 경로·쿼리 값 중 식별자 (퍼센트 인코딩을 푼 값)."""
    base, _, query = path.partition("?")
    values = [unquote(s) for s in base.split("/") if s]
    values += [unquote(kv.partition("=")[2]) for kv in query.split("&") if "=" in kv]
    out = []
    for v in values:
        if is_identifier(v):
            out.append(v)
        else:
            out += identifiers(v)
    return list(dict.fromkeys(out))


def unseen(seen: Seen, values: list[str]) -> list[str]:
    return [v for v in values if not seen.has(v)]


# ───────────────────────── 도구 결과 한 줄 요약

_NAME_KEYS = ("display_name", "name", "title", "file_name", "filename", "label", "code", "key")
_STATUS = re.compile(r"→ HTTP (\d{3})")


def _short(v: Any, n: int = 40) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


def _names(items: list[Any], k: int = 3) -> str:
    """목록 항목의 이름 몇 개. 짧은 목록(12개 이하)은 전부 — 줄이면 모델이 빠진 이름을 지어 채운다 (QA S-13: 그룹 4개 중
    셋만 남기고 '…' 로 줄인 요약을 보고 넷째를 다른 이름으로 채움)."""
    if len(items) <= 12:
        k = len(items)
    dicts = [i for i in items if isinstance(i, dict)]
    key = next((key for key in _NAME_KEYS if any(isinstance(d.get(key), str) and d.get(key) for d in dicts)), "")
    if key:
        vals = [_short(d[key], 48) for d in dicts[:k] if isinstance(d.get(key), str)]
    else:
        vals = [_short(i, 30) for i in items[:k] if not isinstance(i, (dict, list))]
    if not vals:
        return ""
    return " [" + ", ".join(vals) + (", …" if len(items) > k else "") + "]"


def summarize(data: Any) -> str:
    """JSON 값을 한 줄로: 이름 → 수·상태 → 짧은 값 → 목록 개수 순."""
    if isinstance(data, list):
        return f"목록 {len(data)}개{_names(data)}"
    if not isinstance(data, dict):
        return _short(data, 120)
    named, numbers, others, nested = [], [], [], []
    for k, v in data.items():
        if isinstance(v, list):
            nested.append(f"{k}: {len(v)}개{_names(v)}")
        elif isinstance(v, dict):
            inner = ", ".join(f"{a}={_short(b, 24)}" for a, b in list(v.items())[:4] if not isinstance(b, (dict, list)))
            if inner:
                nested.append(f"{k}: {{{inner}}}")
        elif k in _NAME_KEYS and isinstance(v, str):
            named.append(f"{k}={_short(v, 60)}")
        elif isinstance(v, (int, float, bool)) or v is None:
            numbers.append(f"{k}={json.dumps(v)}")
        elif isinstance(v, str) and len(v) <= 60:
            others.append(f"{k}={v}")
    return ", ".join(named + numbers + nested + others)


def _json_part(text: str) -> Any:
    body = re.split(r"\n\n(?:\(응답 |ℹ |\(\[)", text, maxsplit=1)[0]
    m = re.search(r"^[\[{]", body, re.M)
    if not m:
        return None
    raw = body[m.start(): m.start() + 20000]
    try:
        return json.loads(raw)
    except ValueError:
        pass
    try:
        import json_repair
        data = json_repair.loads(raw)
        return data if isinstance(data, (dict, list)) else None
    except Exception:   # noqa: BLE001 — 요약은 실패해도 된다
        return None


def _spread_counts(text: str) -> list[str]:
    """도구의 자동 집계 줄 (이 응답) 과 누계 줄. 누계는 응답 몇 개를 합친 것인지 붙인다 (앞쪽 요약의 중간 누계와 헷갈리지 않게)."""
    own: list[str] = []
    total: list[str] = []
    block, many = "", ""
    for line in text.split("\n\n{", 1)[0].split("\n\n[", 1)[0].splitlines():
        if line.startswith("집계 ("):   # count_by 로 센 줄
            own.append(line.split(" — ")[0])
            continue
        if not line.startswith("  "):   # 머리 줄: 어느 묶음인지
            block = "total" if line.startswith("누계") else "own" if line.startswith("자동 집계") else ""
            m = re.search(r"응답 (\d+)개", line)
            many = m.group(1) if m else ""
            continue
        s = line.strip()
        if not block or "→" not in s or identifiers(s.split(":", 1)[-1][:60]):
            continue   # UUID 같은 참조 값의 개수는 보고에 쓰이지 않는다
        if block == "own":
            own.append(s)
        elif s.count("→") >= 2:
            total.append(f"누계(응답 {many}개) {s}")
    return own[:3]   # 누계는 넣지 않는다 — 한 줄 요약 안에서 이 응답의 값과 헷갈린다 (QA 재현: 누계를 항목 값으로 옮겨 씀)


def _index_names(text: str, k: int = 10) -> str:
    """api_get 결과 앞의 '목록 색인' 에서 앞쪽 k 개 (응답 순서)."""
    m = re.search(r"목록 색인 — ([^\n]*?) (\d+)개의 (\w+)[^\n]*\n((?:  \d+\) [^\n]*\n?)+)", text)
    if not m:
        return ""
    names = [re.sub(r"^\s*\d+\)\s*", "", x) for x in m.group(4).strip("\n").splitlines()[:k]]
    return f"{m.group(1)} {m.group(2)}개, 앞에서부터 {m.group(3)}: " + ", ".join(f"{i}) {n}" for i, n in enumerate(names, 1))


def call_label(name: str, args: dict[str, Any]) -> str:
    if name in API_TOOLS:
        method = "GET" if name == "api_get" else str(args.get("method") or "SEND")
        return f"{method} {args.get('path', '')}"
    main = next((str(args[k]) for k in ("path", "url", "query", "command", "code", "name") if args.get(k)), "")
    return f"{name}({_short(main, 60)})" if main else name


def digest(name: str, args: dict[str, Any], ok: bool, output: str) -> str:
    """도구 결과 한 줄 요약. 실패는 실패라고 쓴다 (실패 응답에서 값을 만들지 않게)."""
    text = output or ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    label = call_label(name, args)
    if name in API_TOOLS and args.get("offset"):   # 이어 읽기는 그 호출의 결과가 아니라 한 조각이다 (QA S-14)
        return f"{label} (이어 읽기 offset={args['offset']}) → 앞 호출 응답의 일부" + ("" if text.strip() else " (응답 끝, 더 없음)")
    status = _STATUS.search(first)
    if not ok:
        why = f"HTTP {status.group(1)}" if status else re.sub(r"^(오류|차단됨):\s*", "", first)[:90]
        return f"{label} → 실패 ({why}) — 값 없음"[:LINE]
    if name in API_TOOLS:
        if index := _index_names(text):   # 긴 목록: 보이는 쪽만이 아니라 도구가 뽑은 색인(응답 순서)으로
            return f"{label} → {status.group(1) if status else '?'}: {index}"[:LINE_LIST]
        data = _json_part(text)
        asked = unquote(str(args.get("path", ""))).lower()
        if isinstance(data, dict):   # 요청 값을 되돌려 준 필드는 뺀다 (경로에 이미 있다)
            data = {k: v for k, v in data.items() if not (isinstance(v, str) and len(v) > 3 and v.lower() in asked)}
        body = summarize(data) if data is not None else re.sub(r"\s+", " ", text.split("\n", 1)[-1]).strip()[:120]
        counts = "; ".join(_spread_counts(text))
        line = f"{label} → {status.group(1) if status else '?'}: " + (f"[집계] {counts} | " if counts else "") + body
        return line[:LINE]
    rest = re.sub(r"\s+", " ", text).strip()
    return f"{label} → {rest[:140]}"[:LINE]


def results_summary(ledger: list[Any], limit: int = SUMMARY_MAX, focus: list[str] | None = None) -> str:
    """이번 실행의 API 결과 요약 (같은 호출은 마지막 것). 되돌려 보낼 때 모델이 실제 값으로 고쳐 쓰게.
    focus: 이 글자가 경로에 든 호출을 먼저 (걸린 항목의 실제 결과가 한도에 잘리지 않게)."""
    lines: dict[str, str] = {}
    for st in ledger:
        if st.name in API_TOOLS and not st.args.get("offset"):   # 이어 읽기가 앞 호출의 결과를 덮지 않게 (QA S-14: 빈 끝 조각이
            line = digest(st.name, st.args, st.ok, st.output)      # '?:' 로 남아 성공한 조회를 실패로 보고함)
            lines.pop(line.split(" → ")[0], None)
            lines[line.split(" → ")[0]] = line
    out, size = [], 0
    items = list(lines.values())
    if focus:
        first = [x for x in items if any(f in unquote(x.split(" → ")[0]) for f in focus)]
        items = first + [x for x in items if x not in first]
    for i, line in enumerate(items):
        if size + len(line) > limit:
            out.append(f"- … 외 {len(items) - i}건 (필요하면 다시 조회)")
            break
        out.append(f"- {line}")
        size += len(line) + 3
    return "\n".join(out)


# ───────────────────────── 답·보고 파일 대조

_CITED = re.compile(r"(?<![\w:/\\.\-])(/[A-Za-z][A-Za-z0-9_\-.{}~%+]*(?:/[A-Za-z0-9_\-.{}~%+:]+)*/?(?:\?[A-Za-z0-9_\-.=&%+{}:,]*)?)")


def cited_paths(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for m in _CITED.finditer(text or "")))


def _base(path: str) -> str:
    return unquote(path.split("?", 1)[0]).rstrip("/").lower() or "/"


def _concrete(path: str) -> bool:
    """실제 값이 들어간 경로인지 (숫자가 든 조각이나 값이 있는 쿼리). 일반적인 경로 이름·틀({id})은 아니다."""
    base, _, query = path.partition("?")
    if "{" in path:
        return False
    return "=" in query or any(any(c.isdigit() for c in seg) for seg in base.split("/") if seg)


def _same(a: str, b: str) -> bool:
    """경로 조각 단위로 같거나 한쪽이 다른 쪽의 끝 (API 기본 경로를 붙이거나 뺀 표기)."""
    if a == b:
        return True
    pa, pb = a.strip("/").split("/"), b.strip("/").split("/")
    short, long_ = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
    return len(short) >= 2 and long_[-len(short):] == short


def _was_sent(st: Any) -> bool:
    """서버에 실제로 보낸 API 호출 (응답을 받았거나, 이어 읽기였거나, 보냈는데 시간 초과·연결 실패). 보내기 전에 막힌 것은 아니다."""
    out = st.output or ""
    return st.name in API_TOOLS and ("→ HTTP " in out or bool(st.args.get("offset")) or "API 호출 실패" in out)


def ungrounded_paths(text: str, ledger: list[Any], documented: Any = None) -> list[str]:
    """글에 적은 API 경로 중 이번 실행에서 보낸 적이 없는 것 (값이 든 경로이거나 문서에 없는 경로만 본다).
    documented(path) → 문서에 있는 경로인지 (모르면 None)."""
    sent = [_base(st.args.get("path", "")) for st in ledger if _was_sent(st) and isinstance(st.args.get("path"), str)]
    out = []
    for p in cited_paths(text):
        if "{" in p or "..." in p or "…" in text[text.find(p): text.find(p) + len(p) + 1]:
            continue   # 틀({id})이나 줄여 쓴 경로(/a/.../b)는 근거 주장으로 보지 않는다
        b = _base(p)
        if any(_same(b, s) for s in sent):
            continue
        undocumented = documented is not None and documented(p.split("?", 1)[0]) is False
        if _concrete(p) or undocumented:
            out.append(p)
    return out


_NUM = re.compile(r"(?<![A-Za-z0-9_.+\-])\d+(?:\.\d+)?(?![A-Za-z0-9_])")
_ROW_INDEX = re.compile(r"^\s*(?:\|\s*)?(?:[#*>\-]\s*)*\**\d{1,3}\**\s*(?:[|.)\]:]|번\b)")


def _own_part(output: str) -> str:
    """도구 결과에서 누계 묶음을 뺀 것 (누계는 여러 응답을 합친 값이라 한 대상의 값이 아니다)."""
    keep, skip = [], False
    for line in output.splitlines():
        if line.startswith("누계"):
            skip = True
            continue
        if skip and (line.startswith("  ") or not line.strip()):
            continue
        skip = False
        keep.append(line)
    return "\n".join(keep)


def row_mismatches(text: str, ledger: list[Any]) -> list[tuple[str, list[str], bool]]:
    """보고에서 한 대상(식별자)을 적은 줄의 숫자가, 그 대상을 경로에 넣어 조회한 결과에 없는지.
    → [(식별자, 결과에 없는 숫자들, 그 대상의 조회가 모두 실패였는지)]. 줄 맨 앞의 순번은 보지 않는다."""
    calls = [(_leaf(str(st.args.get("path", ""))), st.ok, st.output or "") for st in ledger
             if _was_sent(st) and ("→ HTTP " in (st.output or "") or st.args.get("offset"))]
    out: list[tuple[str, list[str], bool]] = []
    done: set[str] = set()
    for line in text.splitlines():
        # 한 대상(경로의 마지막 식별자 = 그 조회의 대상)에 대한 줄만 본다. 앞쪽 범위(앨범·데이터셋 ID)는 대상이 아니다.
        named = [i for i in identifiers(line) if any(leaf and i in leaf for leaf, _, _ in calls)]
        if len(named) != 1 or named[0] in done:
            continue
        ident = named[0]
        hits = [(ok, o) for leaf, ok, o in calls if leaf and ident in leaf]
        rest = _ROW_INDEX.sub("", line)
        for v in identifiers(rest):
            rest = rest.replace(v, " ")
        nums = list(dict.fromkeys(_NUM.findall(rest)))
        if not nums:
            continue
        good = [o for ok, o in hits if ok]
        says_failed = bool(_FAILED_WORDS.search(rest))
        if not good and says_failed:
            continue   # 실패한 대상을 실패로 쓴 줄 (예: "없음 (HTTP 404)")
        pool = good + ([o for ok, o in hits if not ok] if says_failed else [])
        allowed = set(_NUM.findall("\n".join(_own_part(o) for o in pool)))
        bad = [n for n in nums if n not in allowed]
        if bad or not good:
            out.append((ident, bad or nums, not good))
            done.add(ident)
    return out


_FAILED_WORDS = re.compile(r"실패|없(?:음|습니다|어|다)|찾을\s*수\s*없|못\s*(?:함|했|찾)|거절|오류|권한|not\s*found|failed|error|"
                           r"\b(?:40[0-9]|5\d\d)\b", re.I)


def _leaf(path: str) -> str:
    """조회 경로의 대상: 뒤에서부터 처음 나오는 식별자 조각 (파일명·ID). 없으면 빈 글자."""
    for seg in reversed([unquote(s) for s in path.split("?")[0].split("/") if s]):
        if is_identifier(seg) or re.search(r"\.[A-Za-z]\w{1,4}$", seg) or identifiers(seg):
            return seg
    return ""


def check(text: str, ledger: list[Any], seen: Seen, documented: Any = None, where: str = "보고") -> list[str]:
    """글(최종 답 또는 보고 파일)을 실행 기록과 대조해 걸린 것을 문장으로."""
    problems = []
    paths = ungrounded_paths(text, ledger, documented)
    if paths:
        listed = ", ".join(f"'{p}'" for p in paths[:4])
        problems.append(f"{where}에 근거로 적은 경로 {listed} 는 이번 실행에서 호출한 적이 없습니다 — 실제로 부른 경로와 "
                        "그 결과만 근거로 쓸 수 있습니다")
    ids = [v for v in identifiers(text) if not any(v in p or p.endswith(v) for p in paths)]
    fake = unseen(seen, ids)
    if fake:
        listed = ", ".join(f"'{_short(v, 70)}'" for v in fake[:4])
        more = f" 외 {len(fake) - 4}개" if len(fake) > 4 else ""
        problems.append(f"{where}에 적은 값 {listed}{more} 은(는) 이번 실행에서 받은 어떤 결과에도 없습니다 (실패·빈 응답 제외) — "
                        "지어냈거나 실패한 조회에서 만든 값입니다")
    rows = [r for r in row_mismatches(text, ledger) if r[0] not in fake]
    failed = [r for r in rows if r[2]]
    wrong = [r for r in rows if not r[2]]
    if failed:
        listed = ", ".join(f"'{_short(i, 50)}'" for i, _, _ in failed[:3])
        problems.append(f"{where}에서 {listed} 줄의 값은 조회가 실패(거절·빈 결과)한 대상의 것입니다 — 결과가 아니라 실패로 쓰세요")
    if wrong:
        listed = "; ".join(f"'{_short(i, 50)}' 줄의 {', '.join(n[:6])}" for i, n, _ in wrong[:4])
        more = f" 외 {len(wrong) - 4}줄" if len(wrong) > 4 else ""
        problems.append(f"{where}의 값이 그 대상을 조회한 결과와 다릅니다: {listed}{more} — 각 대상의 결과(아래 요약)에 있는 값으로 "
                        "고치세요. '누계'는 여러 응답을 합친 값이라 한 대상의 값이 아닙니다")
    for p in false_failures(text, ledger)[:3]:
        problems.append(f"{where}에서 '{p}' 를 실패로 썼지만 이번 실행에서 그 조회는 성공했습니다 (HTTP 200) — 성공한 결과를 실패로 "
                        "쓰지 말고 그 결과로 그 단계를 하세요 (아래 요약에 그 결과가 있습니다)")
    for extra, missing, label, names in swapped_names(text, ledger)[:2]:
        problems.append(f"{where}의 이름 목록에서 '{extra}' 는 '{label}' 결과에 없는 이름입니다 — 그 결과의 이름은 "
                        f"{', '.join(names[:12])} 입니다 ('{missing}' 이 빠졌습니다)")
    return problems


_SAYS_FAILED = re.compile(r"실패|못\s*(?:했|함|하였|합니다|했습니다)|할\s*수\s*없|빈\s*값|응답이\s*없|오류|\bfailed\b|\berror\b", re.I)


def _path_rx(path: str) -> re.Pattern:
    parts = re.split(r"\{[^}]*\}", path.rstrip("/"))
    return re.compile("^" + "[^/]+".join(map(re.escape, parts)) + "/?$", re.I)


def false_failures(text: str, ledger: list[Any]) -> list[str]:
    """'실패했다'고 쓴 줄의 경로가 이번 실행에서 성공(HTTP 2xx)했고, 같은 경로가 실패한 적은 없으면 그 경로 (QA S-14: 근거 점검에
    막힌 뒤 성공한 4단계 조회를 '응답이 빈 값이나 실패'로 바꿔 보고)."""
    good, bad = set(), set()
    for st in ledger:
        path = st.args.get("path")
        if st.name not in API_TOOLS or st.args.get("offset") or not isinstance(path, str):
            continue
        (good if st.ok and re.search(r"→ HTTP 2\d\d", st.output or "") else bad).add(_base(path))
    out = []
    for line in text.splitlines():
        if not _SAYS_FAILED.search(line):
            continue
        for p in cited_paths(line):
            rx = _path_rx(unquote(p.split("?", 1)[0]))
            if any(rx.match(b) for b in good) and not any(rx.match(b) for b in bad):
                out.append(p)
    return list(dict.fromkeys(out))


def _list_names(ledger: list[Any]) -> list[tuple[str, list[str]]]:
    """성공한 API 결과마다 주 목록 항목의 이름들 (3개 이상인 것)."""
    out = []
    for st in ledger:
        if st.name not in API_TOOLS or not st.ok or st.args.get("offset"):
            continue
        data = _json_part(st.output or "")
        items = data if isinstance(data, list) else max((v for v in data.values() if isinstance(v, list)), key=len, default=[]) \
            if isinstance(data, dict) else []
        dicts = [i for i in items if isinstance(i, dict)]
        key = next((k for k in _NAME_KEYS if sum(isinstance(d.get(k), str) for d in dicts) >= max(3, len(dicts) * 0.9)), "")
        if key:
            out.append((call_label(st.name, st.args), [d[key] for d in dicts if isinstance(d.get(key), str)]))
    return out


def swapped_names(text: str, ledger: list[Any]) -> list[tuple[str, str, str, list[str]]]:
    """한 결과의 이름 목록을 옮겨 적은 줄에서 그 목록의 이름 하나가 빠지고 목록에 없는 이름이 대신 들어간 경우
    → [(들어간 이름, 빠진 이름, 결과, 그 결과의 이름들)] (QA S-13: barcode·labeling·station·station_frame 을 '… station, Dataset_A' 으로)."""
    lists = _list_names(ledger)
    out = []
    for line in text.splitlines():
        plain = re.sub(r"[`*_]{2,}|`", "", line)
        plain = re.sub(r"\([^)]*\)", "", plain.split(":", 1)[-1])
        parts = [s.strip(" .\"'") for s in re.split(r"[,，、/·]|\s(?:및|와|과|and)\s", plain) if s.strip(" .\"'")]
        if len(parts) < 3:
            continue
        for label, names in lists:
            low = {n.lower() for n in names}
            hit = [s for s in parts if s.lower() in low]
            extra = [s for s in parts if s.lower() not in low and re.fullmatch(r"[\w.\-+ ]{2,60}", s) and len(s.split()) <= 3]
            missing = [n for n in names if n.lower() not in {s.lower() for s in parts}]
            if len(hit) >= 2 and len(extra) == 1 and missing and len(parts) == len(hit) + 1 and len(names) == len(parts):
                out.append((extra[0], missing[0], label, names))
                break
    return out


def flagged_items(problems: list[str]) -> list[str]:
    """걸린 문장에 나온 대상 이름 (되돌려 보낼 때 그 대상의 결과 요약을 먼저 보여 준다)."""
    return [m.rstrip("…") for p in problems for m in re.findall(r"'([^']+)' 줄", p)]
