import argparse
import socket
import threading
import time
import webbrowser

import uvicorn


def open_when_ready(url: str, port: int, timeout: float = 60) -> None:
    """서버가 실제로 접속을 받기 시작한 뒤에 브라우저를 연다 (먼저 열면 '연결 거부'가 뜬다)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.3)
    print(f"관리 서버가 {timeout:.0f}초 안에 뜨지 않았습니다. 이 창의 오류 메시지를 확인하세요.")


def already_running(port: int) -> str:
    """그 포트에서 이미 무엇이 듣고 있는지: '' = 비어 있음 · 'agent' = 이 관리 서버 · 'other' = 다른 프로그램.
    uvicorn 은 포트를 잡기 전에 앱 시작 처리(모델 서버 이어받기 등)를 먼저 하므로, 겹치면 그 전에 멈춰야 한다."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    except OSError:
        return ""
    import httpx
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/v1/auth/me", timeout=5)
        return "agent" if r.headers.get("content-type", "").startswith("application/json") else "other"
    except httpx.HTTPError:
        return "other"


def recover(args: argparse.Namespace) -> None:
    """서버 PC 에서만 하는 계정 복구 (관리자 비밀번호를 잊었을 때). 서버가 켜져 있어도 된다."""
    from agent import accounts
    from agent.config import load_settings
    store = accounts.AccountStore(load_settings().data_path / "accounts.db")
    if args.reset_password:
        user = store.by_username(args.reset_password)
        if user is None:
            raise SystemExit(f"없는 아이디입니다: {args.reset_password}")
        temp = store.reset_password(user.id, handled_by="(서버 PC 명령)")
        print(f"'{user.username}' 의 임시 비밀번호: {temp}")
        print("로그인하면 새 비밀번호로 바꾸라는 화면이 나옵니다.")
    if args.login:
        access = accounts.load_access()
        access.login_enabled = args.login == "on"   # 관리자가 없으면 서버 PC 화면에 '첫 관리자 만들기'가 뜬다
        accounts.save_access(access)
        print("로그인 사용: " + ("켬" if access.login_enabled else "끔 (혼자 쓰기 — 이 PC 에서 로그인 없이)"))


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="준비되면 브라우저 열기")
    ap.add_argument("--reset-password", metavar="아이디", help="그 계정의 임시 비밀번호를 만들어 보여 주고 끝낸다 (서버 PC 에서)")
    ap.add_argument("--login", choices=["on", "off"], help="로그인 사용을 켜거나 끄고 끝낸다 (off = 혼자 쓰기)")
    args = ap.parse_args()
    if args.reset_password or args.login:
        recover(args)
        return
    url = f"http://127.0.0.1:{args.port}"
    running = already_running(args.port)
    if running == "agent":   # 이미 켜져 있다 (작업 스케줄러로 숨겨 띄운 것 등) — 또 띄우지 않고 화면만 연다
        print(f"관리 서버가 이미 켜져 있습니다: {url}" + (" — 화면을 엽니다." if args.open else ""))
        if args.open:
            webbrowser.open(url)
        return
    if running == "other":
        raise SystemExit(f"포트 {args.port} 를 다른 프로그램이 쓰고 있습니다. 그 프로그램을 끄거나 --port 로 다른 포트를 고르세요.")
    if args.open:
        threading.Thread(target=open_when_ready, args=(url, args.port), daemon=True).start()
    print(f"에이전트 관리 화면: {url}  (끄려면 이 창에서 Ctrl+C 또는 창 닫기 — 모델 서버도 같이 꺼집니다)")
    # Windows 에서 --reload 를 쓰면 asyncio 서브프로세스가 깨지므로 쓰지 않는다. 워커는 1개.
    uvicorn.run("server.app:app", host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
