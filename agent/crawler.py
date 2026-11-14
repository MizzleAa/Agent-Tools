"""사내 웹사이트 수집기. 시작 주소에서 링크를 따라가며 페이지 본문을 색인(agent/index.py)에 넣는다.

    .venv\\Scripts\\python cli.py crawl            # 등록된 사이트 전부
    .venv\\Scripts\\python cli.py crawl wiki       # 하나만
    (관리 화면 '상태' 탭의 '지금 수집' 버튼도 같은 함수를 쓴다)
"""

from __future__ import annotations

import html
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

from agent.config import IntranetSource, Settings
from agent.index import DocIndex
from agent.tools.base import ToolError, decode_bytes
from agent.tools.web import _download, _extract, auth_headers

HREF = re.compile(r"""href\s*=\s*["']([^"'#\s]+)""", re.I)
SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|svg|ico|webp|css|js|zip|7z|rar|gz|exe|msi|mp4|mp3|avi|woff2?|ttf|"
                      r"pdf|docx?|xlsx?|pptx?|hwp)$", re.I)


@dataclass
class CrawlStatus:
    source: str
    state: str = "running"          # running | done | cancelled | error
    pages: int = 0                  # 색인에 넣은 페이지
    changed: int = 0                # 새로 들어왔거나 내용이 바뀐 페이지
    removed: int = 0                # 사라져서 색인에서 뺀 페이지
    errors: int = 0
    last_error: str = ""
    current_url: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def normalize(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))   # #조각 제거


def in_scope(url: str, source: IntranetSource) -> bool:
    if urlsplit(url).scheme not in ("http", "https"):
        return False
    if SKIP_EXT.search(urlsplit(url).path):
        return False
    if any(pattern in url for pattern in source.exclude_patterns):
        return False
    return any(url.startswith(normalize(p)) or url.startswith(p) for p in source.prefixes())


def crawl(settings: Settings, source: IntranetSource, status: CrawlStatus | None = None,
          cancel: threading.Event | None = None, on_page: Callable[[CrawlStatus], None] | None = None) -> CrawlStatus:
    status = status or CrawlStatus(source.name)
    cancel = cancel or threading.Event()
    index = DocIndex(settings.data_path / "index.db")
    try:
        headers = auth_headers(source.auth)
        queue = deque((normalize(u), 0) for u in source.start_urls)
        seen: set[str] = set()
        kept: set[str] = set()
        hit_limit = False
        while queue:
            if cancel.is_set():
                status.state = "cancelled"
                break
            if status.pages >= source.max_pages:
                hit_limit = True
                break
            url, depth = queue.popleft()
            if url in seen or not in_scope(url, source):
                continue
            seen.add(url)
            status.current_url = url
            try:
                final, ctype, raw = _download(settings, url, headers, source.verify_tls)
                title, text, is_html = _extract(ctype, raw, final)
            except ToolError as e:
                status.errors += 1
                status.last_error = f"{url}: {e}"
                continue
            final = normalize(final)
            seen.add(final)
            if text.strip() and in_scope(final, source):
                kept.add(final)
                status.changed += index.upsert(source.name, final, title or final, text)
                status.pages += 1
            if is_html and depth < source.max_depth:
                for href in HREF.findall(decode_bytes(raw)):
                    link = normalize(urljoin(final, html.unescape(href)))
                    if link not in seen and in_scope(link, source):
                        queue.append((link, depth + 1))
            if on_page:
                on_page(status)
            time.sleep(source.delay)
        # 끝까지 다 돈 경우에만 '사라진 페이지'를 지운다 (한도에 걸려 멈췄으면 못 본 페이지가 있을 수 있음)
        if status.state == "running" and not hit_limit:
            status.removed = index.remove_missing(source.name, kept)
        if status.state == "running":
            status.state = "done"
    except Exception as e:
        status.state = "error"
        status.last_error = f"{type(e).__name__}: {e}"
    finally:
        status.current_url = ""
        status.finished_at = datetime.now().isoformat(timespec="seconds")
        index.close()
    return status


# ───────────────────────── 백그라운드 수집 (관리 화면·채팅 도구·CLI 가 같이 쓴다)

_jobs: dict[str, tuple[CrawlStatus, threading.Event, threading.Thread]] = {}


def start_background(settings: Settings, source: IntranetSource) -> CrawlStatus:
    """이미 수집 중이면 그 진행 상황을, 아니면 새로 시작한 수집의 상태를 돌려준다."""
    job = _jobs.get(source.name)
    if job and job[0].state == "running":
        return job[0]
    status, stop = CrawlStatus(source.name), threading.Event()
    thread = threading.Thread(target=crawl, args=(settings, source, status, stop), daemon=True)
    _jobs[source.name] = (status, stop, thread)
    thread.start()
    return status


def job_status(name: str) -> CrawlStatus | None:
    job = _jobs.get(name)
    return job[0] if job else None


def cancel_jobs(name: str = "") -> None:
    for job_name, (status, stop, _) in _jobs.items():
        if (not name or job_name == name) and status.state == "running":
            stop.set()


def wait_jobs(timeout: float | None = None) -> None:
    for _, _, thread in list(_jobs.values()):
        thread.join(timeout)


def running_jobs() -> list[CrawlStatus]:
    return [status for status, _, _ in _jobs.values() if status.state == "running"]
