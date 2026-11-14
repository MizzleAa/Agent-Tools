"""문서 파일을 글로 읽기 — 글자·코드·CSV, 엑셀(xlsx), PDF, 워드(docx). 이미지는 형식·크기만 (내용은 모델이 볼 수 없다).

read_file · fetch_url · search_files · 파일 탐색기 화면이 같은 읽기를 쓴다 (기능 '공유 경로·파일 탐색', 결정 ③).
- xlsx 는 압축된 XML 이라 표준 라이브러리로 읽는다 (추가 패키지 없이 폐쇄망 반입).
- PDF 는 pymupdf, docx 는 python-docx (requirements 에 이미 있다). 없으면 '이 형식을 읽을 도구가 없음' 으로 알린다.
- 너무 큰 파일은 앞부분만 읽고 그렇다고 밝힌다 (16k 컨텍스트를 넘치지 않게 — 도구가 쪽으로 나눠 준다).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from agent import imaging

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".ini", ".cfg", ".conf",
            ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".cs", ".java", ".go", ".rs", ".sql", ".sh", ".bat", ".ps1",
            ".toml", ".css", ".c", ".h", ".cpp", ".hpp", ".xaml", ".csproj", ".sln", ".rst", ".properties"}
OFFICE_EXT = {".xlsx", ".xlsm", ".docx", ".pdf"}
IMAGE_EXT = {".bmp", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff"}
MAX_DOC_BYTES = 50 * 2**20      # 이보다 큰 문서는 읽지 않는다 (크기·종류만)
MAX_TEXT_CHARS = 2_000_000      # 글로 바꾼 결과의 최대 글자 (도구가 쪽으로 나눠 준다)
MAX_SHEET_ROWS = 5000           # 엑셀 시트마다 읽을 최대 행


@dataclass
class Document:
    kind: str                      # text · table · document · image · binary · too_big
    text: str = ""                 # 모델·화면에 줄 글 (CSV 는 파일 그대로, 엑셀은 탭으로 나눈 줄)
    note: str = ""                 # 잘랐거나 못 읽은 이유
    rows: list[list[str]] = field(default_factory=list)   # 표(CSV·엑셀 첫 시트) — 화면 미리보기용
    sheets: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    fmt: str = ""


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in OFFICE_EXT:
        return "document"
    if ext in (".csv", ".tsv"):
        return "table"
    return "text" if ext in TEXT_EXT or not ext else "binary?"


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read(path: Path, limit: int = MAX_TEXT_CHARS) -> Document:
    """파일 하나를 글로. 실패 이유는 note 에 (예외는 OSError 만 그대로 올린다 — 경로·권한·연결 실패는 부르는 쪽이 구분한다)."""
    size = path.stat().st_size
    ext = path.suffix.lower()
    if size > MAX_DOC_BYTES and ext not in TEXT_EXT:
        return Document("too_big", note=f"파일이 너무 큽니다 ({size // 2**20}MB) — 크기·종류만 알 수 있습니다")
    if ext in IMAGE_EXT:
        with path.open("rb") as f:
            head = f.read(64 * 1024)
        fmt = imaging.sniff(head) or ext.lstrip(".")
        w, h = imaging.size_of(head, fmt) or (0, 0)
        return Document("image", note="이미지 파일 — 내용은 글로 읽을 수 없습니다 (화면 미리보기만)", width=w, height=h, fmt=fmt)
    if ext in (".xlsx", ".xlsm"):
        return _xlsx(path, limit)
    if ext == ".docx":
        return _docx(path, limit)
    if ext == ".pdf":
        return _pdf(path, limit)
    with path.open("rb") as f:
        data = f.read(min(size, limit * 4) + 1)
    if b"\x00" in data[:8192]:
        return Document("binary", note=f"글자가 아닌 파일입니다 ({ext or '확장자 없음'}) — 미리보기 불가")
    text = decode(data[: limit * 4])
    note = f"앞 {len(text)}자만 읽음 (파일 {size // 1024}KB)" if len(data) > limit * 4 or len(text) > limit else ""
    text = text[:limit]
    if ext in (".csv", ".tsv"):
        rows = list(csv.reader(io.StringIO(text), delimiter="\t" if ext == ".tsv" else _sniff_delimiter(text)))
        return Document("table", text=text, rows=rows[:1000], note=note)   # 글은 파일 그대로 (코드 작업이 원문을 본다)
    return Document("text", text=text, note=note)


def from_bytes(raw: bytes, suffix: str, limit: int = MAX_TEXT_CHARS) -> Document:
    """내려받은 문서(사내 공유 링크 등)를 글로 — 임시 파일로 써서 같은 방법으로 읽는다."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / f"doc{suffix}"
        f.write_bytes(raw)
        return read(f, limit)


SUFFIX_FOR_TYPE = {"application/pdf": ".pdf",
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx", "text/csv": ".csv"}


def _sniff_delimiter(text: str) -> str:
    head = text[:4096]
    return max((",", ";", "\t", "|"), key=head.count)


# ───────────────────────── 엑셀 (xlsx: 압축된 XML)

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx(path: Path, limit: int) -> Document:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        return Document("binary", note="엑셀 파일을 열 수 없습니다 (손상됐거나 암호가 걸린 파일)")
    with z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")) for si in root.findall("m:si", _NS)]
        book = ElementTree.fromstring(z.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels")) if "xl/_rels/workbook.xml.rels" in names else None
        targets = {r.get("Id"): r.get("Target") for r in rels} if rels is not None else {}
        out, first_rows, sheet_names, note = [], [], [], ""
        for sheet in book.findall("m:sheets/m:sheet", _NS):
            name = sheet.get("name") or "Sheet"
            target = targets.get(sheet.get(f"{{{_NS['r']}}}id"), "")
            member = "xl/" + target.lstrip("/").removeprefix("xl/") if target else ""
            if member not in names:
                continue
            sheet_names.append(name)
            rows = []
            for row in ElementTree.fromstring(z.read(member)).iter(f"{{{_NS['m']}}}row"):
                cells: dict[int, str] = {}
                for c in row.findall("m:c", _NS):
                    v = c.find("m:v", _NS)
                    value = v.text if v is not None else "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
                    if c.get("t") == "s" and value is not None and value.isdigit() and int(value) < len(shared):
                        value = shared[int(value)]
                    cells[_col(c.get("r", "A1"))] = value or ""
                if cells:
                    rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
                if len(rows) >= MAX_SHEET_ROWS:
                    note = f"시트마다 앞 {MAX_SHEET_ROWS}행만 읽음"
                    break
            if not first_rows:
                first_rows = rows[:1000]
            out.append(f"## 시트: {name} ({len(rows)}행)\n" + "\n".join("\t".join(r) for r in rows))
    text = "\n\n".join(out)
    if len(text) > limit:
        text, note = text[:limit], f"앞 {limit}자만 읽음"
    return Document("table", text=text, rows=first_rows, sheets=sheet_names, note=note)


# ───────────────────────── 워드 · PDF

def _docx(path: Path, limit: int) -> Document:
    try:
        import docx  # python-docx
    except ImportError:
        return Document("binary", note="워드 파일을 읽을 도구(python-docx)가 설치돼 있지 않습니다")
    try:
        d = docx.Document(str(path))
    except Exception as e:   # noqa: BLE001 — 손상·암호 문서
        return Document("binary", note=f"워드 파일을 열 수 없습니다 ({type(e).__name__})")
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        parts.append("\n".join("\t".join(c.text.strip() for c in row.cells) for row in t.rows))
    text = "\n".join(parts)
    return Document("document", text=text[:limit], note=f"앞 {limit}자만 읽음" if len(text) > limit else "")


def _pdf(path: Path, limit: int) -> Document:
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # 예전 이름
        except ImportError:
            return Document("binary", note="PDF 를 읽을 도구(pymupdf)가 설치돼 있지 않습니다")
    try:
        doc = pymupdf.open(str(path))
    except Exception as e:   # noqa: BLE001
        return Document("binary", note=f"PDF 를 열 수 없습니다 ({type(e).__name__})")
    parts, size = [], 0
    with doc:
        for i, page in enumerate(doc, 1):
            t = page.get_text()
            parts.append(f"--- {i}쪽 ---\n{t}")
            size += len(t)
            if size > limit:
                break
        pages = doc.page_count
    text = "\n".join(parts)
    note = f"{pages}쪽 중 {len(parts)}쪽까지 읽음" if len(parts) < pages else ""
    if not text.strip():
        note = "글자가 없는 PDF 입니다 (스캔 이미지일 수 있음 — 글로 읽을 수 없음)"
    return Document("document", text=text[:limit], note=note)
