"""로그인 사용 모드의 사용자 계정 · 회사 · 로그인 세션 · 비밀번호 재설정 요청 (data/accounts.db),
접속 설정(config/access.yaml), 사용자별 사내 API 계정(config/user_secrets.yaml).

- 접속 방식은 두 가지다. 기본은 '로그인 사용'(들어오면 로그인 화면부터 — 관리자가 고객사별로 계정을 발급하고, 각자 로그인해서
  자기 대화·작업 폴더만 본다). 관리자가 일부러 끄면 '혼자 쓰기'(login_enabled=false — 이 서버에 들어온 사람 = 관리자).
  관리자 계정이 하나도 없으면(처음 설치) 로그인 화면 대신 '첫 관리자 만들기'가 뜨고, 그것은 서버 PC 에서만 할 수 있다.
- 비밀번호는 scrypt 로 만든 값만 저장한다 (원문 저장 없음). 로그인 세션은 쿠키의 무작위 값이고 DB 에는 그 해시만 둔다.
- 임시 비밀번호(발급·재설정)로 들어오면 바꾸기 전에는 다른 기능을 쓸 수 없다. 마지막으로 바꾼 지
  password_max_age_days(기본 30일)가 지나면 로그인할 때 바꾸라고 안내한다 ('나중에' 가능).
- 실행 스레드·도구는 CURRENT 로 '지금 누구의 일인지'를 안다 (사내 API 본인 계정, 공유 위치 허용, 관리 도구 제한).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from agent import config

ROLE_ADMIN, ROLE_USER = "admin", "user"
MIN_PASSWORD = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{1,47}$")
_TEMP_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"   # 헷갈리는 글자(0·O·1·l·I) 뺌


# ───────────────────────── 접속 설정 (config/access.yaml)

class Access(BaseModel):
    app_name: str = Field(default="AI Agent", min_length=1, max_length=40)   # 화면에 보이는 서비스 이름
    login_enabled: bool = True             # 기본 = 로그인 먼저. 끄면 혼자 쓰기
    public_hosts: list[str] = Field(default_factory=list)   # 다른 PC 가 이 서버에 들어올 때 쓰는 주소 (사내 IP·도메인)
    password_max_age_days: int = Field(default=30, ge=0, le=3650)   # 0 = 안내 안 함
    session_hours: int = Field(default=12, ge=1, le=24 * 30)
    offline: bool = False             # 폐쇄망 스위치: 외부 AI 모델(OpenAI·Claude·Gemini)을 모두 끈다


def access_file() -> Path:
    return config.CONFIG_DIR / "access.yaml"


_access_cache: dict[str, tuple[float, Access]] = {}


def load_access() -> Access:
    """요청마다 부르므로 파일이 바뀌었을 때만 다시 읽는다 (직접 고쳐도 바로 적용)."""
    path = access_file()
    mtime = path.stat().st_mtime if path.exists() else -1.0
    cached = _access_cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1].model_copy(deep=True)
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    access = Access.model_validate(raw)
    _access_cache[str(path)] = (mtime, access)
    return access.model_copy(deep=True)


def save_access(access: Access) -> None:
    path = access_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _access_cache.pop(str(path), None)
    path.write_text("# 접속 설정 (관리 화면 '관리 › 접속 설정'이 저장한다). 직접 고쳐도 된다 — 서버를 다시 켜지 않아도 바로 적용.\n"
                    "# login_enabled: false 로 바꾸면 로그인 없이 이 PC 에서 쓰는 '혼자 쓰기'로 돌아간다 (관리자 비밀번호를 잊었을 때).\n"
                    + yaml.safe_dump(access.model_dump(), allow_unicode=True, sort_keys=False), encoding="utf-8")


# ───────────────────────── 비밀번호

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(base64.b64decode(digest)))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def temp_password() -> str:
    return "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(10))


def check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다")
    if password.strip() != password:
        raise ValueError("비밀번호 앞뒤에 공백을 넣을 수 없습니다")


# ───────────────────────── 모양

@dataclass
class Company:
    id: str
    name: str
    note: str = ""
    seats: int | None = None          # 그 회사가 요청한(허용된) 계정 수. 비우면 제한 없음
    profiles: list[str] = field(default_factory=list)   # 쓸 수 있는 용도(프로필). 비우면 전부
    created_at: str = ""


@dataclass
class User:
    id: str
    username: str
    display_name: str = ""
    company_id: str = ""
    role: str = ROLE_USER
    active: bool = True
    must_change: bool = False         # 임시 비밀번호 — 바꾸기 전에는 다른 기능을 못 쓴다
    password_changed_at: str = ""
    created_at: str = ""
    last_login_at: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResetRequest:
    id: str
    username: str
    company: str = ""
    note: str = ""
    user_id: str = ""                 # 아이디가 실제로 있으면 그 사용자
    status: str = "pending"           # pending · done · dismissed
    created_at: str = ""
    handled_at: str = ""
    handled_by: str = ""


# 지금 이 요청·실행이 누구의 것인지. None = 로그인 없이 쓰는 '혼자 쓰기'(이 PC 사람 = 관리자)
CURRENT: ContextVar[User | None] = ContextVar("agent_user", default=None)


def current() -> User | None:
    return CURRENT.get()


def is_admin(user: User | None) -> bool:
    return user is None or user.is_admin


def owner_id(user: User | None) -> str:
    return "" if user is None else user.id


def _now() -> datetime:
    return datetime.now()


def _iso(t: datetime | None = None) -> str:
    return (t or _now()).isoformat(timespec="seconds")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


SCHEMA = """
create table if not exists companies (
    id text primary key, name text not null unique, note text not null default '',
    seats integer, profiles text not null default '[]', created_at text not null
);
create table if not exists users (
    id text primary key, username text not null unique collate nocase, display_name text not null default '',
    company_id text not null default '', role text not null default 'user', active integer not null default 1,
    password_hash text not null, must_change integer not null default 0, password_changed_at text not null,
    created_at text not null, last_login_at text not null default ''
);
create table if not exists sessions (
    token_hash text primary key, user_id text not null, created_at text not null, expires_at text not null
);
create table if not exists reset_requests (
    id text primary key, username text not null, company text not null default '', note text not null default '',
    user_id text not null default '', status text not null default 'pending', created_at text not null,
    handled_at text not null default '', handled_by text not null default ''
);
"""


class AccountStore:
    """계정 DB (data/accounts.db). 웹 요청 스레드들이 같이 쓰므로 잠금으로 직렬화한다."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("pragma journal_mode=wal")
            self.db.executescript(SCHEMA)

    # ── 회사

    @staticmethod
    def _company(row: sqlite3.Row) -> Company:
        return Company(row["id"], row["name"], row["note"], row["seats"], json.loads(row["profiles"] or "[]"), row["created_at"])

    def companies(self) -> list[Company]:
        with self.lock:
            return [self._company(r) for r in self.db.execute("select * from companies order by name")]

    def company(self, company_id: str) -> Company | None:
        with self.lock:
            row = self.db.execute("select * from companies where id=?", (company_id,)).fetchone()
        return self._company(row) if row else None

    def save_company(self, name: str, note: str = "", seats: int | None = None, profiles: list[str] | None = None,
                     company_id: str = "") -> Company:
        name = name.strip()
        if not name:
            raise ValueError("회사 이름을 넣으세요")
        if seats is not None and seats < 0:
            raise ValueError("계정 수는 0 이상이어야 합니다")
        if seats is not None and company_id and seats < self.seat_count(company_id):
            raise ValueError(f"이미 발급한 계정({self.seat_count(company_id)}개)보다 적게 줄일 수 없습니다")
        with self.lock, self.db:
            if self.db.execute("select 1 from companies where name=? and id<>?", (name, company_id)).fetchone():
                raise ValueError(f"같은 이름의 회사가 있습니다: {name}")
            data = (name, note.strip(), seats, json.dumps(sorted(set(profiles or [])), ensure_ascii=False))
            if company_id:
                if not self.db.execute("update companies set name=?, note=?, seats=?, profiles=? where id=?",
                                       (*data, company_id)).rowcount:
                    raise KeyError("회사가 없습니다")
            else:
                company_id = uuid.uuid4().hex[:10]
                self.db.execute("insert into companies(id, name, note, seats, profiles, created_at) values (?,?,?,?,?,?)",
                                (company_id, *data, _iso()))
        return self.company(company_id)  # type: ignore[return-value]

    def delete_company(self, company_id: str) -> None:
        with self.lock, self.db:
            if self.db.execute("select 1 from users where company_id=?", (company_id,)).fetchone():
                raise ValueError("이 회사에 계정이 남아 있습니다. 계정을 먼저 지우거나 다른 회사로 옮기세요.")
            if not self.db.execute("delete from companies where id=?", (company_id,)).rowcount:
                raise KeyError("회사가 없습니다")

    def seat_count(self, company_id: str) -> int:
        with self.lock:
            return self.db.execute("select count(*) from users where company_id=?", (company_id,)).fetchone()[0]

    # ── 사용자

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(row["id"], row["username"], row["display_name"], row["company_id"], row["role"], bool(row["active"]),
                    bool(row["must_change"]), row["password_changed_at"], row["created_at"], row["last_login_at"])

    def users(self) -> list[User]:
        with self.lock:
            return [self._user(r) for r in self.db.execute("select * from users order by company_id, username")]

    def user(self, user_id: str) -> User | None:
        with self.lock:
            row = self.db.execute("select * from users where id=?", (user_id,)).fetchone()
        return self._user(row) if row else None

    def by_username(self, username: str) -> User | None:
        with self.lock:
            row = self.db.execute("select * from users where username=?", (username.strip(),)).fetchone()
        return self._user(row) if row else None

    def admin_count(self) -> int:
        with self.lock:
            return self.db.execute("select count(*) from users where role='admin' and active=1").fetchone()[0]

    def create_user(self, username: str, *, display_name: str = "", company_id: str = "", role: str = ROLE_USER,
                    password: str | None = None) -> tuple[User, str]:
        """계정을 만든다. password 를 안 주면 임시 비밀번호를 만들어 돌려준다 (첫 로그인 때 바꿔야 함)."""
        username = username.strip()
        if not USERNAME_RE.match(username):
            raise ValueError("아이디는 영문·숫자로 시작하고 영문·숫자·. _ @ - 만 쓸 수 있습니다 (2~48자)")
        if role not in (ROLE_ADMIN, ROLE_USER):
            raise ValueError("역할은 admin 또는 user 입니다")
        if company_id:
            company = self.company(company_id)
            if company is None:
                raise ValueError("회사를 찾을 수 없습니다")
            if company.seats is not None and self.seat_count(company_id) >= company.seats:
                raise ValueError(f"'{company.name}'의 허용 계정 수({company.seats}개)를 모두 발급했습니다. "
                                 "회사 정보에서 계정 수를 늘린 뒤 발급하세요.")
        if password is not None:
            check_new_password(password)
        secret = password if password is not None else temp_password()
        user = User(uuid.uuid4().hex[:12], username, display_name.strip(), company_id, role, True,
                    password is None, _iso(), _iso())
        with self.lock, self.db:
            if self.db.execute("select 1 from users where username=?", (username,)).fetchone():
                raise ValueError(f"이미 있는 아이디입니다: {username}")
            self.db.execute(
                "insert into users(id, username, display_name, company_id, role, active, password_hash, must_change,"
                " password_changed_at, created_at) values (?,?,?,?,?,?,?,?,?,?)",
                (user.id, user.username, user.display_name, user.company_id, user.role, 1, hash_password(secret),
                 int(user.must_change), user.password_changed_at, user.created_at))
        return user, ("" if password is not None else secret)

    def update_user(self, user_id: str, *, display_name: str | None = None, company_id: str | None = None,
                    role: str | None = None, active: bool | None = None) -> User:
        user = self.user(user_id)
        if user is None:
            raise KeyError("사용자가 없습니다")
        if role is not None and role not in (ROLE_ADMIN, ROLE_USER):
            raise ValueError("역할은 admin 또는 user 입니다")
        losing_admin = user.is_admin and user.active and (role == ROLE_USER or active is False)
        if losing_admin and self.admin_count() <= 1:
            raise ValueError("마지막 관리자는 일반 사용자로 바꾸거나 끌 수 없습니다")
        if company_id is not None and company_id != user.company_id and company_id:
            company = self.company(company_id)
            if company is None:
                raise ValueError("회사를 찾을 수 없습니다")
            if company.seats is not None and self.seat_count(company_id) >= company.seats:
                raise ValueError(f"'{company.name}'의 허용 계정 수({company.seats}개)가 다 찼습니다")
        changes = {k: v for k, v in (("display_name", display_name), ("company_id", company_id), ("role", role),
                                     ("active", None if active is None else int(active))) if v is not None}
        if changes:
            with self.lock, self.db:
                self.db.execute(f"update users set {', '.join(f'{k}=?' for k in changes)} where id=?",
                                (*changes.values(), user_id))
                if active is False:
                    self.db.execute("delete from sessions where user_id=?", (user_id,))
        return self.user(user_id)  # type: ignore[return-value]

    def delete_user(self, user_id: str) -> None:
        user = self.user(user_id)
        if user is None:
            raise KeyError("사용자가 없습니다")
        if user.is_admin and user.active and self.admin_count() <= 1:
            raise ValueError("마지막 관리자는 지울 수 없습니다")
        with self.lock, self.db:
            self.db.execute("delete from sessions where user_id=?", (user_id,))
            self.db.execute("delete from users where id=?", (user_id,))

    def reset_password(self, user_id: str, handled_by: str = "") -> str:
        """임시 비밀번호를 새로 만들어 돌려준다. 그 사람의 로그인 세션은 모두 끝나고, 대기 중인 재설정 요청은 처리됨."""
        if self.user(user_id) is None:
            raise KeyError("사용자가 없습니다")
        secret = temp_password()
        with self.lock, self.db:
            self.db.execute("update users set password_hash=?, must_change=1, password_changed_at=? where id=?",
                            (hash_password(secret), _iso(), user_id))
            self.db.execute("delete from sessions where user_id=?", (user_id,))
            self.db.execute("update reset_requests set status='done', handled_at=?, handled_by=? "
                            "where user_id=? and status='pending'", (_iso(), handled_by, user_id))
        return secret

    def change_password(self, user_id: str, current_password: str, new_password: str, keep_token: str = "") -> User:
        """본인이 바꾼다. 다른 곳에 열려 있던 로그인은 끝나고 지금 세션(keep_token)만 남는다."""
        with self.lock:
            row = self.db.execute("select password_hash from users where id=?", (user_id,)).fetchone()
        if row is None:
            raise KeyError("사용자가 없습니다")
        if not verify_password(current_password, row["password_hash"]):
            raise ValueError("지금 비밀번호가 맞지 않습니다")
        check_new_password(new_password)
        if new_password == current_password:
            raise ValueError("새 비밀번호가 지금 비밀번호와 같습니다")
        with self.lock, self.db:
            self.db.execute("update users set password_hash=?, must_change=0, password_changed_at=? where id=?",
                            (hash_password(new_password), _iso(), user_id))
            self.db.execute("delete from sessions where user_id=? and token_hash<>?", (user_id, _sha(keep_token)))
        return self.user(user_id)  # type: ignore[return-value]

    @staticmethod
    def password_expired(user: User, max_age_days: int) -> bool:
        if max_age_days <= 0 or user.must_change or not user.password_changed_at:
            return False
        return _now() - datetime.fromisoformat(user.password_changed_at) > timedelta(days=max_age_days)

    # ── 로그인 세션

    def login(self, username: str, password: str, hours: int) -> tuple[User, str]:
        with self.lock:
            row = self.db.execute("select * from users where username=?", (username.strip(),)).fetchone()
        if row is None or not verify_password(password, row["password_hash"]):
            raise ValueError("아이디 또는 비밀번호가 맞지 않습니다")
        if not row["active"]:
            raise ValueError("사용이 중지된 계정입니다. 관리자에게 문의하세요.")
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self.db.execute("insert into sessions(token_hash, user_id, created_at, expires_at) values (?,?,?,?)",
                            (_sha(token), row["id"], _iso(), _iso(_now() + timedelta(hours=hours))))
            self.db.execute("update users set last_login_at=? where id=?", (_iso(), row["id"]))
            self.db.execute("delete from sessions where expires_at < ?", (_iso(),))
        return self.user(row["id"]), token  # type: ignore[return-value]

    def session_user(self, token: str, hours: int) -> User | None:
        """세션이 살아 있으면 그 사용자. 쓰는 동안은 만료 시각을 늦춘다 (마지막 사용부터 hours 시간)."""
        if not token:
            return None
        key = _sha(token)
        with self.lock:
            row = self.db.execute("select * from sessions where token_hash=?", (key,)).fetchone()
        if row is None or row["expires_at"] < _iso():
            return None
        user = self.user(row["user_id"])
        if user is None or not user.active:
            return None
        renew = _now() + timedelta(hours=hours)
        if datetime.fromisoformat(row["expires_at"]) < renew - timedelta(minutes=5):
            with self.lock, self.db:
                self.db.execute("update sessions set expires_at=? where token_hash=?", (_iso(renew), key))
        return user

    def logout(self, token: str) -> None:
        with self.lock, self.db:
            self.db.execute("delete from sessions where token_hash=?", (_sha(token),))

    # ── 비밀번호 찾기 (관리자에게 재설정 요청)

    @staticmethod
    def _request(row: sqlite3.Row) -> ResetRequest:
        return ResetRequest(**{k: row[k] for k in row.keys()})

    def request_reset(self, username: str, company: str = "", note: str = "") -> None:
        """로그인 화면의 '비밀번호 찾기'. 아이디가 없어도 같은 답을 준다 (있는 아이디를 캐내지 못하게)."""
        username = username.strip()[:64]
        if not username:
            raise ValueError("아이디를 넣으세요")
        user = self.by_username(username)
        with self.lock, self.db:
            if user and self.db.execute("select 1 from reset_requests where user_id=? and status='pending'",
                                        (user.id,)).fetchone():
                return   # 이미 대기 중
            self.db.execute("insert into reset_requests(id, username, company, note, user_id, created_at) values (?,?,?,?,?,?)",
                            (uuid.uuid4().hex[:10], username, company.strip()[:100], note.strip()[:300],
                             user.id if user else "", _iso()))

    def reset_requests(self, pending_only: bool = False) -> list[ResetRequest]:
        sql = "select * from reset_requests" + (" where status='pending'" if pending_only else "") + " order by created_at desc limit 200"
        with self.lock:
            return [self._request(r) for r in self.db.execute(sql)]

    def close_request(self, request_id: str, handled_by: str, status: str = "dismissed") -> None:
        with self.lock, self.db:
            if not self.db.execute("update reset_requests set status=?, handled_at=?, handled_by=? where id=?",
                                   (status, _iso(), handled_by, request_id)).rowcount:
                raise KeyError("요청이 없습니다")


# ───────────────────────── 사용자별 사내 API 계정 (config/user_secrets.yaml)

def user_secrets_file() -> Path:
    return config.CONFIG_DIR / "user_secrets.yaml"


_secret_lock = threading.Lock()


def _read_user_secrets() -> dict[str, dict[str, str]]:
    path = user_secrets_file()
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def get_user_secret(user_id: str, name: str) -> str | None:
    value = (_read_user_secrets().get(user_id) or {}).get(name)
    return None if value is None else str(value)


def set_user_secret(user_id: str, name: str, value: str | None) -> None:
    with _secret_lock:
        data = _read_user_secrets()
        mine = data.setdefault(user_id, {})
        if value:
            mine[name] = value
        else:
            mine.pop(name, None)
        if not mine:
            data.pop(user_id, None)
        path = user_secrets_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 사용자별 사내 API 계정 (로그인 사용 모드). git·오프라인 번들에 포함되지 않는다.\n"
                        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def drop_user_secrets(user_id: str) -> None:
    with _secret_lock:
        data = _read_user_secrets()
        if data.pop(user_id, None) is not None:
            user_secrets_file().write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def personal_secrets() -> str | None:
    """사내 API 로그인 계정을 사람마다 따로 쓰는지: 로그인 사용 모드에서 실행 중인 사용자가 있으면 그 사람 id."""
    user = current()
    return user.id if user is not None and load_access().login_enabled else None


# ───────────────────────── 사용자별 작업 폴더

def folder_name(user: User) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", user.username)[:48] or user.id


def user_workspace(workspace: Path, user: User) -> Path:
    """로그인 사용 모드에서 그 사람의 작업 폴더 (workspace/users/<아이디>)."""
    return workspace / "users" / folder_name(user)
