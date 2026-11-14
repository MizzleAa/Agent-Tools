"""코드가 요구의 '실제 동작'을 하는지 가볍게 본다 — 근거 점검(evidence.py)이 쓴다 (QA E4·E5·E7·E10).

- location_symbols / referenced : 검증 표의 '구현 위치(파일:함수)'와, 근거 테스트가 실제로 부르는 이름 (E5)
- stub_functions               : 몸통이 출력(print)·값 저장뿐인 '껍데기' 함수 (E4)
- import_only                  : import 만 해 보고 끝나는 확인 코드 (E4, 규격: import 성공은 증거가 아님)
- fake_reason                  : 확장자는 영상·이미지·문서인데 실제 형식이 아닌 파일 (E7)

과제와 무관한 일반 규칙만 둔다. Python 은 AST 로, JavaScript/TypeScript·C#·Java 는 가벼운 정규식으로 본다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PY = (".py", ".pyw")
BRACE = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".cs", ".java", ".kt")

# ───────────────────────── E5: 구현 위치의 함수 · 테스트가 부르는 이름

_FILE_TOKEN = re.compile(r"(?:[\w.\-]+[/\\])*[\w\-][\w.\-]*\.(?:pyw?|m?js|cjs|tsx?|jsx|cs|java|kt|go|rs|rb|php|vue|swift|"
                         r"c|cpp|h|hpp|html?|css|json|md|txt|toml|ya?ml|xaml|csproj|sln|ps1|sh|bat)\b", re.I)
_SYMBOL = re.compile(r"(?P<pre>::|[:#.]\s*)?(?P<name>[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*(?P<call>\(\s*\))?")
_NOT_SYMBOL = {"class", "def", "function", "method", "module", "file", "and", "or", "the", "src", "tests", "test", "lib",
               "py", "js", "ts", "cs", "ui", "gui", "api", "cli", "tbd", "none", "n", "a"}


def location_symbols(location: str) -> list[str]:
    """'main.py:seek()' → ['seek'] · 'player.py::Player.play' → ['play'] · 'Player.cs: Play(), Pause()' → ['Play', 'Pause'].
    파일 경로만 적었으면 [] (함수를 알 수 없음)."""
    text = _FILE_TOKEN.sub(" ", location)
    out: list[str] = []
    for m in _SYMBOL.finditer(text):
        name = re.split(r"\s*\.\s*", m.group("name"))[-1]
        camel = any(c.isupper() for c in name[1:]) or "_" in name.strip("_")
        if name.lower() in _NOT_SYMBOL or len(name) < 2:
            continue
        if m.group("call") or m.group("pre") or camel or (name[0].isupper() and len(name) >= 4):
            out.append(name)
    return list(dict.fromkeys(out))


def _strip_code(source: str) -> str:
    """주석·문자열을 지운다 (괄호 안 이름·테스트 설명 문장이 이름으로 잡히지 않게)."""
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    source = re.sub(r"(?m)//.*$", " ", source)
    return re.sub(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`", '""', source)


def referenced(source: str, suffix: str) -> set[str]:
    """코드가 부르거나 가리키는 이름 (정의한 함수 이름·설명 문자열은 뺀다)."""
    if suffix in PY:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return set(re.findall(r"[A-Za-z_]\w*", source))
        names: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
            elif isinstance(n, ast.alias):
                names.add((n.asname or n.name).split(".")[-1])
                names.add(n.name.split(".")[-1])
            elif isinstance(n, ast.Call):   # getattr(obj, "seek") · subprocess 인자의 '-m pkg'
                for a in [*n.args, *(k.value for k in n.keywords)]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        names.update(re.findall(r"[A-Za-z_]\w*", a.value))
        return names
    return set(re.findall(r"[A-Za-z_$][\w$]*", _strip_code(source)))


def defined_symbols(source: str, suffix: str) -> set[str]:
    """파일이 정의하는 함수·메서드·클래스 이름."""
    if suffix in PY:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return set()
        return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    return {name for name, _ in _brace_functions(source)} | set(re.findall(r"\b(?:class|interface|struct|enum)\s+(\w+)", source))


# ───────────────────────── E4: 껍데기 함수

_LOG_ATTR = {"debug", "info", "warning", "warn", "error", "critical", "log", "print", "exception"}


def _py_simple(v: ast.AST | None) -> bool:
    return v is None or isinstance(v, (ast.Constant, ast.Name, ast.Attribute)) or (
        isinstance(v, ast.UnaryOp) and isinstance(v.operand, ast.Constant))


def _py_stub_body(body: list[ast.stmt]) -> tuple[bool, bool]:
    """(출력·값 저장·pass 로만 되어 있는가, 출력·미구현 표시가 있는가)."""
    marked = False
    for st in body:
        if isinstance(st, ast.Pass) or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)):
            continue
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            f = st.value.func
            if (isinstance(f, ast.Name) and f.id == "print") or (isinstance(f, ast.Attribute) and f.attr in _LOG_ATTR):
                marked = True
                continue
            return False, marked
        if isinstance(st, ast.Assign) and _py_simple(st.value) or isinstance(st, ast.AnnAssign) and _py_simple(st.value):
            continue
        if isinstance(st, ast.Return) and _py_simple(st.value):
            continue
        if isinstance(st, ast.Raise) and "NotImplemented" in ast.unparse(st):
            marked = True
            continue
        if isinstance(st, ast.If):
            ok_a, m_a = _py_stub_body(st.body)
            ok_b, m_b = _py_stub_body(st.orelse)
            if not (ok_a and ok_b):
                return False, marked
            marked = marked or m_a or m_b
            continue
        return False, marked
    return True, marked


def _py_stubs(source: str) -> tuple[set[str], set[str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), set()
    funcs, stubs = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not (n.name.startswith("__") and n.name.endswith("__")):
            funcs.add(n.name)
            only, marked = _py_stub_body(n.body)
            empty = all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                        for s in n.body)
            if only and (marked or empty):
                stubs.add(n.name)
    return funcs, stubs


_BRACE_SIG = re.compile(
    r"(?:\bfunction\s+(?P<f>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::\s*[\w<>\[\], |]+)?\s*\{)|"
    r"(?:(?P<a>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{)|"
    r"(?:^[ \t]*(?:(?:public|private|protected|internal|static|virtual|override|async|abstract|sealed|final|export|"
    r"readonly)\s+)*(?:[\w<>\[\],.?]+\s+)?(?P<m>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::\s*[\w<>\[\], |]+)?\s*\{)", re.M)
_KEYWORDS = {"if", "for", "while", "switch", "catch", "foreach", "using", "lock", "return", "function", "else", "do", "try",
             "constructor", "new", "base", "this", "super"}
_PRINT = re.compile(r"^(?:console\.(?:log|info|warn|error|debug)|Console\.Write(?:Line)?|Debug\.(?:Log|WriteLine)|"
                    r"System\.out\.print(?:ln)?|print|alert|logger\.\w+|_logger\.\w+|Log\.\w+)\s*\(", re.I)
_STORE = re.compile(r"^(?:(?:var|let|const)\s+)?[\w$.]+\s*=\s*(?:\"\"|true|false|null|undefined|-?\d[\d.]*[fFdDmM]?|[\w$.]+)$")
_RETURN = re.compile(r"^return(?:\s+(?:\"\"|true|false|null|undefined|-?\d[\d.]*|[\w$.]+))?$")
_NOTIMPL = re.compile(r"^throw\s+new\s+(?:NotImplementedException|NotSupportedException|UnsupportedOperationException)\b")


def _brace_functions(source: str) -> list[tuple[str, str]]:
    """(이름, 몸통) — 중괄호 짝으로 몸통을 자른다."""
    clean = _strip_code(source)
    out = []
    for m in _BRACE_SIG.finditer(clean):
        name = m.group("f") or m.group("a") or m.group("m")
        if not name or name in _KEYWORDS:
            continue
        depth, i = 0, m.end() - 1
        for j in range(i, len(clean)):
            depth += {"{": 1, "}": -1}.get(clean[j], 0)
            if depth == 0:
                out.append((name, clean[i + 1:j]))
                break
    return out


def _brace_stub(body: str) -> bool:
    text = re.sub(r"\b(?:if|else\s+if)\s*\([^)]*\)|\belse\b|[{}]", ";", body)
    stmts = [s.strip() for s in re.split(r"[;\n]", text) if s.strip()]
    marked = False
    for s in stmts:
        if _PRINT.match(s) or _NOTIMPL.match(s):
            marked = True
        elif not (_STORE.match(s) or _RETURN.match(s)):
            return False
    return marked or not stmts


def stub_functions(path: Path) -> tuple[set[str], set[str]]:
    """(정의된 함수, 그중 몸통이 출력·값 저장·pass·미구현 예외뿐인 함수). 모르는 언어는 빈 집합."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(), set()
    suffix = path.suffix.lower()
    if suffix in PY:
        return _py_stubs(source)
    if suffix in BRACE:
        funcs = _brace_functions(source)
        return {n for n, _ in funcs}, {n for n, b in funcs if _brace_stub(b)}
    return set(), set()


# ───────────────────────── E4: import 만 해 보는 확인

_PY_C = re.compile(r"(?:python3?|py)(?:\.exe)?[\"']?\s+(?:-\w+\s+)*-c\s+(\"(?:\\.|[^\"])*\"|'(?:[^'])*')", re.I)


def import_only(code: str) -> bool:
    """import·sys.path 조작·print 만 있는 파이썬 코드 (빈 코드는 아님)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    has_import = False
    for st in tree.body:
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            has_import = True
        elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            f = st.value.func
            if not ((isinstance(f, ast.Name) and f.id == "print") or
                    (isinstance(f, ast.Attribute) and f.attr in ("insert", "append") and "sys.path" in ast.unparse(f))):
                return False
        elif not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)):
            return False
    return has_import


def command_python_code(command: str) -> str | None:
    """'python -c "…"' 의 코드 부분."""
    m = _PY_C.search(command)
    return m.group(1)[1:-1].replace('\\"', '"') if m else None


# ───────────────────────── E7: 실제 형식이 아닌 파일

def _ftyp(b: bytes) -> bool:
    return b[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip", b"pnot")


def _riff(kind: bytes):
    return lambda b: b[:4] == b"RIFF" and b[8:12] == kind


_MAGIC = {
    ".mp4": _ftyp, ".m4v": _ftyp, ".m4a": _ftyp, ".mov": _ftyp, ".3gp": _ftyp,
    ".avi": _riff(b"AVI "), ".wav": _riff(b"WAVE"), ".webp": _riff(b"WEBP"),
    ".mkv": lambda b: b[:4] == b"\x1a\x45\xdf\xa3", ".webm": lambda b: b[:4] == b"\x1a\x45\xdf\xa3",
    ".mp3": lambda b: b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0),
    ".flac": lambda b: b[:4] == b"fLaC", ".ogg": lambda b: b[:4] == b"OggS", ".ogv": lambda b: b[:4] == b"OggS",
    ".opus": lambda b: b[:4] == b"OggS", ".flv": lambda b: b[:3] == b"FLV",
    ".wmv": lambda b: b[:4] == b"\x30\x26\xb2\x75", ".wma": lambda b: b[:4] == b"\x30\x26\xb2\x75",
    ".png": lambda b: b[:8] == b"\x89PNG\r\n\x1a\n", ".jpg": lambda b: b[:3] == b"\xff\xd8\xff",
    ".jpeg": lambda b: b[:3] == b"\xff\xd8\xff", ".gif": lambda b: b[:4] == b"GIF8", ".bmp": lambda b: b[:2] == b"BM",
    ".pdf": lambda b: b[:4] == b"%PDF", ".zip": lambda b: b[:2] == b"PK", ".docx": lambda b: b[:2] == b"PK",
    ".xlsx": lambda b: b[:2] == b"PK", ".pptx": lambda b: b[:2] == b"PK", ".gz": lambda b: b[:2] == b"\x1f\x8b",
}
MEDIA_EXT = frozenset(_MAGIC)


def fake_reason(path: Path) -> str | None:
    """확장자가 말하는 형식이 아니면 그 이유 (빈 파일 · 0 바이트로 채움 · 공백 · 글자 파일 · 형식 표시 없음)."""
    check = _MAGIC.get(path.suffix.lower())
    if check is None:
        return None
    try:
        with path.open("rb") as f:
            head = f.read(64)
    except OSError:
        return None
    if not head:
        return "빈 파일"
    if check(head):
        return None
    if not head.strip(b"\x00"):
        return "내용이 0 바이트로만 채워짐"
    if not head.strip():
        return "내용이 공백뿐"
    try:
        head.decode("utf-8")
        return "글자로 쓴 파일"
    except UnicodeDecodeError:
        return "형식 표시(파일 앞부분)가 없음"
