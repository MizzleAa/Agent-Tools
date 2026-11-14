"""공유 위치 — 사용자가 채팅에 준 네트워크 공유 경로·매핑 드라이브·file:// 링크를 읽기 전용으로 허용하는 목록.

기능 '공유 경로·파일 탐색' (결정 ① 읽기 전용 · ② 로컬만 · ④ 계정은 관리 화면 · ⑤ 목록 하나 · ⑥ 모든 용도가 읽기로 공유).
- 처음 보는 위치는 사용자 확인(승인 창)을 거쳐서만 목록에 들어간다. 범위는 '그 폴더와 그 아래' 이고, 파일을 주면 그 파일이 든 폴더.
- 목록은 config/shares.yaml 에 저장된다 (관리 화면 '상태 · 연결' 에서 보고 지운다). 모든 용도의 샌드박스가 읽기 전용으로 쓴다.
- 시스템 폴더, 에이전트 자신의 폴더(설정·비밀 저장소·모델·기록), 드라이브 전체는 목록에 넣을 수 없다 (그것을 품는 범위도).
- 경로는 에이전트가 도는 이 PC 기준이다 — 원격 사용자의 자기 PC 경로가 아니다.
- 공유에 별도 계정이 필요하면 관리 화면에서 넣는다 (비밀 저장소에만 — 모델·채팅·기록·주소에 남지 않는다).
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

import yaml

from agent import config

PROBE_SECONDS = 8.0     # 연결 안 되는 서버에서 멈추지 않게 (A6: 연결 안 됨)


def store_file() -> Path:
    return config.CONFIG_DIR / "shares.yaml"


# ───────────────────────── 경로 모양

_DEVICE = re.compile(r"^(?:\\\\|//)[?.](?:\\|/)")
_UNC = re.compile(r"^(?:\\\\|//)[^\\/?.:*\"<>|][^\\/:*\"<>|]*[\\/][^\\/:*\"<>|]+")
_DRIVE_ABS = re.compile(r"^[A-Za-z]:[\\/]")
# 채팅 글 속 위치: \\서버\공유\… · Z:\… · file://… — 띄어쓰기에서 끊는다 (띄어쓰기가 든 경로는 따옴표로 감싼 것만)
_FIND = re.compile(r"\"((?:\\\\|[A-Za-z]:\\|file:/)[^\"\n]+)\"|"
                   r"(\\\\[^\s\\/\"'<>|]+\\[^\s\"'<>|`]+|file:/{2,3}[^\s\"'<>|`]+|(?<![\w/\\])[A-Za-z]:\\[^\s\"'<>|`]*)")


def to_path(raw: str) -> str:
    """file://서버/공유/x → \\\\서버\\공유\\x , file:///Z:/x → Z:\\x . 그 밖은 앞뒤 따옴표만 걷어 그대로."""
    s = (raw or "").strip().strip('"').strip("'").strip("`")
    if s.lower().startswith("file:"):
        rest = unquote(re.sub(r"^file:", "", s, flags=re.I))
        rest = rest.replace("/", "\\")
        if re.match(r"^\\{2,3}[A-Za-z]:", rest):                 # file:///C:/x
            return rest.lstrip("\\")
        if rest.startswith("\\\\\\"):                            # file:////server/share
            rest = rest[1:]
        return "\\\\" + rest.lstrip("\\") if rest.startswith("\\\\") else rest
    return s


def is_unc(path: str) -> bool:
    return bool(_UNC.match(path)) and not _DEVICE.match(path)


def is_device(path: str) -> bool:
    return bool(_DEVICE.match(path))


def is_location(raw: str) -> bool:
    """파일 시스템의 절대 위치(네트워크 공유·드라이브 경로·file://)인지."""
    p = to_path(raw)
    return is_unc(p) or bool(_DRIVE_ABS.match(p))


def find_in_text(text: str) -> list[str]:
    return [(m.group(1) or m.group(2)).rstrip(" .,)") for m in _FIND.finditer(text or "")]


def norm(path: str | Path) -> str:
    """비교용 정규화. 네트워크 경로는 realpath 를 쓰지 않는다 (연결 안 되는 서버에서 오래 멈춘다)."""
    s = str(path)
    if is_unc(s) or _is_remote_drive(s):
        return os.path.normcase(os.path.normpath(s))
    return os.path.normcase(os.path.realpath(s))


def within(child: str, parent: str) -> bool:
    c, p = child.rstrip("\\/"), parent.rstrip("\\/")
    return c == p or c.startswith(p + os.sep)


def _is_remote_drive(path: str) -> bool:
    if not _DRIVE_ABS.match(path) or os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(path[:3]) == 4   # DRIVE_REMOTE
    except Exception:   # noqa: BLE001
        return False


def _is_fixed_drive_root(path: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]:[\\/]?", path.strip())) and not _is_remote_drive(path[:2] + "\\")


# ───────────────────────── 넣을 수 없는 곳

def protected(settings: Any = None) -> list[tuple[str, str]]:
    """(정규화한 경로, 이유) — 이 안이나 이것을 품는 범위는 허용 목록에 넣을 수 없다."""
    out = [(norm(config.ROOT), "에이전트 자신의 폴더(설정·비밀 저장소·모델·실행 기록)"),
           (norm(config.CONFIG_DIR), "에이전트 설정·비밀 저장소"),
           (norm(config.SECRETS_FILE.parent), "에이전트 비밀 저장소")]
    if settings is not None:
        out.append((norm(settings.data_path), "에이전트 실행 기록·데이터"))
    env = os.environ
    for key, why in (("SystemRoot", "시스템 폴더 (Windows)"), ("ProgramFiles", "프로그램 폴더"), ("ProgramFiles(x86)", "프로그램 폴더"),
                     ("ProgramData", "시스템 데이터 폴더"), ("ProgramW6432", "프로그램 폴더")):
        if env.get(key):
            out.append((norm(env[key]), why))
    if env.get("USERPROFILE"):
        out.append((norm(Path(env["USERPROFILE"]) / "AppData"), "사용자 프로그램 설정 폴더 (AppData)"))
    return out


def _this_pc() -> set[str]:
    import socket
    names = {"localhost", "127.0.0.1", "::1", "0.0.0.0", os.environ.get("COMPUTERNAME", "").lower()}
    try:
        host = socket.gethostname()
        names |= {host.lower(), socket.getfqdn().lower(), *socket.gethostbyname_ex(host)[2]}
    except OSError:
        pass
    return {n for n in names if n}


def local_equivalent(path: str) -> str | None:
    """이 PC 를 가리키는 네트워크 경로(\\\\localhost\\C$\\… 등)면 이 PC 의 실제 경로 ('' = 이 PC 의 공유인데 알 수 없음).
    다른 PC 면 None. 이 PC 의 공유로 시스템·에이전트 폴더를 우회해 여는 것을 막는다."""
    parts = [x for x in to_path(path).replace("/", "\\").split("\\") if x]
    if len(parts) < 2:
        return None
    host, share, rest = parts[0].lower(), parts[1], parts[2:]
    if host not in _this_pc() and not re.fullmatch(r"127(?:\.\d{1,3}){3}", host):
        return None
    if re.fullmatch(r"[A-Za-z]\$", share):            # 관리 공유 C$ → C:\
        return os.path.join(share[0].upper() + ":\\", *rest)
    if share.upper() == "ADMIN$":
        return os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), *rest)
    try:   # 이 PC 의 일반 공유: 실제 폴더를 묻는다 (권한이 없으면 알 수 없음 → 허용하지 않는다)
        import ctypes
        from ctypes import wintypes

        class SHARE_INFO_2(ctypes.Structure):
            _fields_ = [("netname", wintypes.LPWSTR), ("type", wintypes.DWORD), ("remark", wintypes.LPWSTR),
                        ("permissions", wintypes.DWORD), ("max_uses", wintypes.DWORD), ("current_uses", wintypes.DWORD),
                        ("path", wintypes.LPWSTR), ("passwd", wintypes.LPWSTR)]

        buf = ctypes.c_void_p()
        if ctypes.windll.netapi32.NetShareGetInfo(None, share, 2, ctypes.byref(buf)) == 0 and buf:
            try:
                local = ctypes.cast(buf, ctypes.POINTER(SHARE_INFO_2)).contents.path or ""
            finally:
                ctypes.windll.netapi32.NetApiBufferFree(buf)
            return os.path.join(local, *rest) if local else ""
    except Exception:   # noqa: BLE001
        pass
    return ""


def refuse_reason(path: str, settings: Any = None) -> str:
    """이 위치를 허용 목록에 넣거나 읽으면 안 되는 이유 (괜찮으면 빈 글자). 작업 폴더는 원래 허용 범위라 여기서 보지 않는다."""
    p = to_path(path)
    if is_device(p):
        return "장치 경로(\\\\?\\·\\\\.\\)는 쓸 수 없습니다"
    if is_unc(p) and (local := local_equivalent(p)) is not None:   # 이 PC 를 가리키는 공유 경로 → 실제 경로로 같은 점검
        if not local:
            return "이 PC 의 공유인데 실제 폴더를 확인할 수 없어 허용하지 않습니다 — 그 폴더의 드라이브 경로(예: D:\\공유\\…)로 주세요"
        return refuse_reason(local, settings)
    if _is_fixed_drive_root(p):
        return "드라이브 전체는 허용할 수 없습니다 — 그 안의 폴더를 주세요"
    n = norm(p)
    work = norm(settings.workspace_path) if settings is not None else ""
    for area, why in protected(settings):
        if within(n, area) and not (work and within(n, work)):
            return f"{why}는 열 수 없습니다"
        if within(area, n):
            return f"{why}를 품는 범위라 허용할 수 없습니다 — 더 안쪽 폴더를 주세요"
    return ""


# ───────────────────────── 허용 목록 (config/shares.yaml)

def _read() -> dict[str, Any]:
    f = store_file()
    if not f.exists():
        return {"shares": []}
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    data.setdefault("shares", [])
    return data


def _write(data: dict[str, Any]) -> None:
    f = store_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# 공유 위치 허용 목록 (읽기 전용). 관리 화면 '상태 · 연결' › 공유 위치에서 보고 지운다.\n"
                 + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def ident(path: str) -> str:
    return hashlib.sha1(os.path.normcase(os.path.normpath(path)).encode("utf-8")).hexdigest()[:12]


def _eid(e: dict[str, Any]) -> str:
    """허용 항목 id. 공용 항목은 경로로만 (예전 id 그대로), 개인 항목은 경로+주인으로 (같은 경로를 여러 사람이 허용할 수 있다)."""
    return ident(e["path"]) if not e.get("owner") else ident(f"{e['path']}|{e['owner']}")


def _me() -> tuple[str, bool]:
    """(지금 사용자 id, 모든 항목을 보는지). 로그인 없이 쓰거나 관리자면 전부 본다.
    로그인 사용 모드의 일반 사용자는 공용 항목(owner 없음)과 자기가 허용한 항목만 쓴다."""
    from agent import accounts
    user = accounts.current()
    return ("", True) if user is None else (user.id, user.is_admin)


def _visible(e: dict[str, Any], me: str, everything: bool) -> bool:
    return everything or e.get("owner", "") in ("", me)


def entries() -> list[dict[str, Any]]:
    me, everything = _me()
    out = []
    for e in _read()["shares"]:
        if isinstance(e, dict) and e.get("path") and _visible(e, me, everything):
            out.append({"id": _eid(e), "path": e["path"], "added": e.get("added", ""),
                        "has_account": bool(e.get("account")), "owner": e.get("owner", "")})
    return out


def paths() -> list[str]:
    return [e["path"] for e in entries()]


def find(path: str) -> dict[str, Any] | None:
    """이 경로를 품는 허용 위치 (없으면 None)."""
    n = norm(to_path(path))
    best = None
    for e in entries():
        if within(n, norm(e["path"])) and (best is None or len(e["path"]) > len(best["path"])):
            best = e
    return best


def add(path: str, settings: Any = None) -> dict[str, Any]:
    p = os.path.normpath(to_path(path))
    if why := refuse_reason(p, settings):
        raise ValueError(why)
    if not is_location(p):
        raise ValueError("네트워크 공유(\\\\서버\\공유\\…)·드라이브 경로(Z:\\…)·file:// 링크만 허용 목록에 넣을 수 있습니다")
    data = _read()
    n = norm(p)
    me, everything = _me()
    owner = "" if everything else me   # 관리자·혼자 쓰기가 허용하면 공용, 일반 사용자가 허용하면 그 사람만
    mine = lambda e: isinstance(e, dict) and e.get("owner", "") in ("", owner)   # noqa: E731
    if any(within(n, norm(e["path"])) for e in data["shares"] if mine(e)):
        return find(p) or entries()[0]
    data["shares"] = [e for e in data["shares"]   # 안쪽 것은 합친다 (같은 주인의 것만)
                      if not (isinstance(e, dict) and e.get("owner", "") == owner and within(norm(e["path"]), n))]
    item = {"path": p, "added": datetime.now().isoformat(timespec="seconds")}
    if owner:
        item["owner"] = owner
    data["shares"].append(item)
    _write(data)
    return find(p) or {}


def remove(share_id: str) -> bool:
    data = _read()
    me, everything = _me()
    keep, gone = [], None
    for e in data["shares"]:
        if gone is None and isinstance(e, dict) and e.get("path") and _eid(e) == share_id and _visible(e, me, everything) \
                and (everything or e.get("owner") == me):   # 일반 사용자는 자기가 허용한 것만 지운다
            gone = e
        else:
            keep.append(e)
    if gone is None:
        return False
    data["shares"] = keep
    _write(data)
    if gone.get("account"):
        _drop_secrets(gone["account"])
    return True


# ───────────────────────── 별도 계정 (관리 화면에서만 넣는다)

def _secret_names(share_id: str) -> dict[str, str]:
    return {"username": f"SHARE_{share_id.upper()}_USERNAME", "password": f"SHARE_{share_id.upper()}_PASSWORD"}


def set_account(share_id: str, username: str, password: str) -> None:
    from agent import connections
    data = _read()
    for e in data["shares"]:
        if isinstance(e, dict) and e.get("path") and _eid(e) == share_id:
            names = _secret_names(share_id)
            connections.set_secret(names["username"], username)
            connections.set_secret(names["password"], password)
            e["account"] = names
            _write(data)
            return
    raise KeyError(share_id)


def clear_account(share_id: str) -> None:
    data = _read()
    for e in data["shares"]:
        if isinstance(e, dict) and e.get("path") and _eid(e) == share_id and e.get("account"):
            _drop_secrets(e.pop("account"))
            _write(data)
            return


def _drop_secrets(names: dict[str, str]) -> None:
    f = config.SECRETS_FILE
    if not f.exists():
        return
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    for n in names.values():
        data.pop(n, None)
    f.write_text("# 토큰·비밀번호. git·오프라인 번들에 포함되지 않는다.\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


def account(path: str) -> tuple[str, str] | None:
    e = find(path)
    if not e or not e.get("has_account"):
        return None
    names = _secret_names(e["id"])
    user, pw = config.get_secret(names["username"]), config.get_secret(names["password"])
    return (user, pw) if user and pw else None


def connect(path: str) -> bool:
    """허용 위치에 계정이 있으면 Windows 공유 연결을 그 계정으로 맺는다 (명령 줄·기록에 비밀번호가 남지 않게 API 로)."""
    creds = account(path)
    p = to_path(path)
    if not creds or not is_unc(p) or os.name != "nt":
        return False
    parts = [x for x in p.replace("/", "\\").split("\\") if x]
    remote = f"\\\\{parts[0]}\\{parts[1]}"
    import ctypes
    from ctypes import wintypes

    class NETRESOURCE(ctypes.Structure):
        _fields_ = [("dwScope", wintypes.DWORD), ("dwType", wintypes.DWORD), ("dwDisplayType", wintypes.DWORD),
                    ("dwUsage", wintypes.DWORD), ("lpLocalName", wintypes.LPWSTR), ("lpRemoteName", wintypes.LPWSTR),
                    ("lpComment", wintypes.LPWSTR), ("lpProvider", wintypes.LPWSTR)]

    nr = NETRESOURCE(0, 1, 0, 0, None, remote, None, None)   # RESOURCETYPE_DISK
    code = ctypes.windll.mpr.WNetAddConnection2W(ctypes.byref(nr), creds[1], creds[0], 0)
    return code in (0, 1219)   # 1219: 이미 다른 계정으로 연결됨


# ───────────────────────── 접근 실패를 구분해 알리기 (A6 · A7)

_NO_PATH = {2, 3, 123, 161}
_NO_SERVER = {53, 59, 64, 67, 121, 1203, 1222, 1231, 1232, 1311}
_NEEDS_ACCOUNT = {86, 1219, 1326, 1327, 1330, 1331, 1385, 1907, 1909, 2242}


def explain(e: BaseException, path: str) -> str:
    """접근 실패를 사용자가 할 일이 다른 갈래로: 경로 없음 · 권한 없음 · 연결 안 됨 · 별도 계정 필요."""
    code = getattr(e, "winerror", None)
    where = "관리 화면 '상태 · 연결' › 공유 위치"
    if isinstance(e, TimeoutError):
        return f"연결 안 됨: '{path}' 가 {PROBE_SECONDS:.0f}초 안에 응답하지 않습니다 — 서버가 꺼졌거나 네트워크가 끊겼을 수 있습니다"
    if code in _NO_SERVER:
        return f"연결 안 됨: '{path}' 의 서버·공유를 찾을 수 없습니다 (네트워크·서버 이름을 확인하세요)"
    if code in _NEEDS_ACCOUNT:
        return f"별도 계정 필요: '{path}' 는 이 PC 계정으로 열리지 않습니다 — {where}에서 이 위치의 계정을 넣으세요 (채팅으로 받지 않습니다)"
    if isinstance(e, FileNotFoundError) or code in _NO_PATH:
        return (f"경로 없음: '{path}' — 에이전트가 도는 이 PC 기준으로 찾았는데 없습니다. 사용자의 다른 PC(원격)에 있는 파일이면 "
                "이 PC 에서는 볼 수 없습니다 — 공유 위치에 두거나 작업 폴더에 올려 주세요")
    if isinstance(e, PermissionError) or code == 5:
        return f"권한 없음: '{path}' 를 이 PC 계정으로 읽을 수 없습니다 (계정이 따로 있으면 {where}에서 넣으세요)"
    return f"읽지 못함: '{path}' ({getattr(e, 'strerror', None) or type(e).__name__})"


def run_with_timeout(fn: Callable[[], Any], seconds: float = PROBE_SECONDS) -> Any:
    """네트워크 위치 접근을 시간 제한 안에서 (연결 안 되는 서버에서 실행 전체가 멈추지 않게)."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:   # noqa: BLE001 — 부른 쪽에서 다시 올린다
            box["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(seconds)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def probe(path: str) -> tuple[str, str]:
    """('dir' | 'file' | 'error', 설명). 계정이 있는 허용 위치면 한 번 연결해 다시 본다."""
    p = to_path(path)

    def stat() -> str:
        return "dir" if os.path.isdir(p) else "file" if os.path.isfile(p) else "missing"

    try:
        kind = run_with_timeout(lambda: (os.stat(p), stat())[1])
    except OSError as e:
        if getattr(e, "winerror", None) in _NEEDS_ACCOUNT | {5} and connect(p):
            try:
                kind = run_with_timeout(stat)
            except (OSError, TimeoutError) as e2:
                return "error", explain(e2, path)
        else:
            return "error", explain(e, path)
    except TimeoutError as e:
        return "error", explain(e, path)
    if kind == "missing":
        return "error", explain(FileNotFoundError(path), path)
    return kind, ""


def scope_for(path: str, kind: str) -> str:
    """허용할 범위: 폴더면 그 폴더, 파일이면 그 파일이 든 폴더."""
    p = os.path.normpath(to_path(path))
    return p if kind == "dir" else os.path.dirname(p)


# ───────────────────────── 새 작업 폴더를 만들 '작업 위치' (관리자가 입력창에서 고른다 — 로컬 에이전트·CLI 공통)
# 기준 폴더 = 환경변수 AGENT_WORK_ROOT (절대 경로, 또는 서버 폴더 기준 상대 경로) · 없으면 그 사람의 작업 폴더(서버 아래 workspace).
# 작업 위치는 기준 아래의 상대 경로로만 준다 ('' = 기준 폴더 자체). 절대 경로·'..'·기준 밖은 받지 않는다.

WORK_ROOT_ENV = "AGENT_WORK_ROOT"
_ABS = re.compile(r"^(?:[A-Za-z]:|[\/])")
_BAD_PART = re.compile(r'[<>:"|?*\x00-\x1f]')


def work_root(settings: Any) -> Path:
    raw = os.environ.get(WORK_ROOT_ENV, "").strip().strip('"')
    if not raw:
        return Path(os.path.realpath(settings.workspace_path))
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = config.ROOT / p
    workspace = norm(settings.workspace_path)
    why = "" if within(norm(p), workspace) else refuse_reason(str(p), settings)
    if why:
        raise ValueError(f"환경변수 {WORK_ROOT_ENV} 의 폴더를 쓸 수 없습니다 — {why}")
    p.mkdir(parents=True, exist_ok=True)
    return Path(os.path.realpath(p))


def _relative_parts(rel: str) -> list[str]:
    raw = (rel or "").strip().strip('"')
    if _ABS.match(raw) or raw.startswith("\\\\"):
        raise ValueError(f"작업 위치는 기준 폴더 아래의 상대 경로로 주세요 (예: 앱/로그인): {raw}")
    parts = [x.strip() for x in re.split(r"[\/]+", raw) if x.strip() not in ("", ".")]
    for x in parts:
        if x == ".." or _BAD_PART.search(x) or x.endswith("."):
            raise ValueError(f"쓸 수 없는 폴더 이름입니다: {x}")
    return parts


def work_base(rel: str, settings: Any, create: bool = True) -> Path:
    """작업 위치(기준 아래 상대 경로) → 절대 경로. create 면 없는 폴더를 만든다. 기준 밖으로 나가면 ValueError."""
    root = work_root(settings)
    p = root.joinpath(*_relative_parts(rel))
    if create:
        p.mkdir(parents=True, exist_ok=True)
    real = Path(os.path.realpath(p))
    if not within(norm(real), norm(root)):   # 바로가기(링크)로 기준 밖을 가리키는 경우
        raise ValueError(f"작업 위치가 기준 폴더({root}) 밖을 가리킵니다")
    return real


def _rel(path: Path, root: Path) -> str:
    return "" if norm(path) == norm(root) else os.path.relpath(path, root).replace("\\", "/")


def browse_work_dirs(rel: str | None, settings: Any) -> dict[str, Any]:
    """작업 위치 고르기 창: 기준 폴더 아래 하위 폴더 (경로는 모두 기준 기준 상대 경로)."""
    root = work_root(settings)
    p = work_base(rel or "", settings, create=False)
    if not p.is_dir():
        raise FileNotFoundError(f"폴더가 없습니다: {_rel(p, root)} (고르면 보낼 때 만듭니다)")
    dirs = []
    try:
        for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if c.is_dir() and not c.name.startswith(("$", ".")):
                dirs.append({"name": c.name, "path": _rel(c, root)})
    except PermissionError:
        pass
    at = _rel(p, root)
    return {"root": str(root), "root_from_env": bool(os.environ.get(WORK_ROOT_ENV, "").strip()), "path": at,
            "parent": None if not at else _rel(p.parent, root), "dirs": dirs[:500]}
