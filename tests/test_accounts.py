"""계정 저장소 (agent/accounts.py): 비밀번호 · 임시 비밀번호 · 30일 변경 안내 · 재설정 요청 · 회사 계정 수 · 마지막 관리자."""

from datetime import datetime, timedelta

import pytest

from agent import accounts
from agent.accounts import AccountStore


@pytest.fixture
def store(tmp_path) -> AccountStore:
    return AccountStore(tmp_path / "accounts.db")


def test_password_is_stored_only_as_scrypt_hash(store, tmp_path):
    user, _ = store.create_user("kim", password="Secr3t!pw", role="admin")
    raw = (tmp_path / "accounts.db").read_bytes()
    assert b"Secr3t!pw" not in raw
    assert accounts.verify_password("Secr3t!pw", accounts.hash_password("Secr3t!pw"))
    assert not accounts.verify_password("wrong-pass", accounts.hash_password("Secr3t!pw"))
    assert not accounts.verify_password("x", "garbage")


def test_issued_account_gets_temp_password_that_must_be_changed(store):
    user, temp = store.create_user("lee")
    assert temp and len(temp) == 10 and user.must_change
    logged, token = store.login("lee", temp, hours=12)
    assert logged.id == user.id and store.session_user(token, 12).id == user.id
    with pytest.raises(ValueError):
        store.change_password(user.id, "not-the-temp", "NewPassw0rd")
    with pytest.raises(ValueError):
        store.change_password(user.id, temp, "short")
    changed = store.change_password(user.id, temp, "NewPassw0rd", keep_token=token)
    assert not changed.must_change
    with pytest.raises(ValueError):
        store.login("lee", temp, 12)
    assert store.login("lee", "NewPassw0rd", 12)[0].id == user.id


def test_changing_password_ends_other_sessions_but_keeps_current(store):
    user, _ = store.create_user("park", password="Passw0rd!1")
    _, here = store.login("park", "Passw0rd!1", 12)
    _, there = store.login("park", "Passw0rd!1", 12)
    store.change_password(user.id, "Passw0rd!1", "Passw0rd!2", keep_token=here)
    assert store.session_user(here, 12) is not None
    assert store.session_user(there, 12) is None


def test_password_expiry_after_max_age(store):
    user, _ = store.create_user("choi", password="Passw0rd!1")
    assert not store.password_expired(user, 30)
    user.password_changed_at = (datetime.now() - timedelta(days=31)).isoformat(timespec="seconds")
    assert store.password_expired(user, 30)
    user.password_changed_at = (datetime.now() - timedelta(days=29)).isoformat(timespec="seconds")
    assert not store.password_expired(user, 30)
    user.password_changed_at = (datetime.now() - timedelta(days=400)).isoformat(timespec="seconds")
    assert not store.password_expired(user, 0)   # 0 = 안내 안 함


def test_reset_request_flow(store):
    user, _ = store.create_user("jung", password="Passw0rd!1")
    _, token = store.login("jung", "Passw0rd!1", 12)
    store.request_reset("jung", "A사", "내선 1234")
    store.request_reset("jung", "A사")          # 대기 중이면 또 쌓지 않는다
    store.request_reset("nobody", "B사")        # 없는 아이디도 같은 답 (요청은 남는다)
    pending = store.reset_requests(pending_only=True)
    assert [r.username for r in pending] == ["nobody", "jung"] or {r.username for r in pending} == {"nobody", "jung"}
    assert next(r for r in pending if r.username == "jung").user_id == user.id
    temp = store.reset_password(user.id, handled_by="admin")
    assert store.session_user(token, 12) is None              # 재설정하면 열려 있던 로그인은 끝난다
    assert store.user(user.id).must_change
    assert {r.username for r in store.reset_requests(pending_only=True)} == {"nobody"}
    assert store.login("jung", temp, 12)
    with pytest.raises(ValueError):
        store.login("jung", "Passw0rd!1", 12)


def test_company_seats_limit_issuing(store):
    company = store.save_company("A사", seats=1, profiles=["general"])
    store.create_user("a1", company_id=company.id)
    with pytest.raises(ValueError, match="허용 계정 수"):
        store.create_user("a2", company_id=company.id)
    with pytest.raises(ValueError, match="적게 줄일 수 없습니다"):
        store.save_company("A사", seats=0, company_id=company.id)
    store.save_company("A사", seats=2, profiles=["general"], company_id=company.id)
    store.create_user("a2", company_id=company.id)
    assert store.seat_count(company.id) == 2
    with pytest.raises(ValueError, match="계정이 남아"):
        store.delete_company(company.id)


def test_last_admin_cannot_be_removed(store):
    admin, _ = store.create_user("boss", password="Passw0rd!1", role="admin")
    with pytest.raises(ValueError):
        store.update_user(admin.id, role="user")
    with pytest.raises(ValueError):
        store.update_user(admin.id, active=False)
    with pytest.raises(ValueError):
        store.delete_user(admin.id)
    store.create_user("boss2", password="Passw0rd!1", role="admin")
    assert store.update_user(admin.id, role="user").role == "user"


def test_inactive_user_cannot_log_in_and_sessions_end(store):
    user, _ = store.create_user("han", password="Passw0rd!1")
    _, token = store.login("han", "Passw0rd!1", 12)
    store.update_user(user.id, active=False)
    assert store.session_user(token, 12) is None
    with pytest.raises(ValueError, match="중지"):
        store.login("han", "Passw0rd!1", 12)


def test_duplicate_and_bad_usernames(store):
    store.create_user("Kim.D", password="Passw0rd!1")
    with pytest.raises(ValueError, match="이미 있는"):
        store.create_user("kim.d", password="Passw0rd!1")   # 대소문자 무시
    for bad in ("", "a", "한글아이디", "a b", "-lead"):
        with pytest.raises(ValueError):
            store.create_user(bad)


def test_access_settings_roundtrip_and_cache():
    a = accounts.load_access()
    assert not a.login_enabled and a.password_max_age_days == 30 and not a.offline
    a.public_hosts = ["10.0.0.5"]
    a.offline = True
    accounts.save_access(a)
    assert accounts.load_access().public_hosts == ["10.0.0.5"] and accounts.load_access().offline
    text = accounts.access_file().read_text(encoding="utf-8")
    accounts.access_file().write_text(text.replace("offline: true", "offline: false"), encoding="utf-8")
    import os
    import time
    later = time.time() + 5
    os.utime(accounts.access_file(), (later, later))
    assert not accounts.load_access().offline   # 직접 고쳐도 바로 적용


def test_user_secrets_are_per_user():
    accounts.set_user_secret("u1", "API_X_USERNAME", "kim")
    accounts.set_user_secret("u2", "API_X_USERNAME", "lee")
    assert accounts.get_user_secret("u1", "API_X_USERNAME") == "kim"
    assert accounts.get_user_secret("u2", "API_X_USERNAME") == "lee"
    accounts.set_user_secret("u1", "API_X_USERNAME", None)
    assert accounts.get_user_secret("u1", "API_X_USERNAME") is None
    accounts.drop_user_secrets("u2")
    assert accounts.get_user_secret("u2", "API_X_USERNAME") is None


def test_user_workspace_folder(tmp_path):
    user = accounts.User(id="abc", username="kim@a.co")
    assert accounts.user_workspace(tmp_path, user) == tmp_path / "users" / "kim_a.co"   # 폴더 이름에 못 쓰는 글자는 _
    assert accounts.folder_name(accounts.User(id="x", username="a/b\\c")) == "a_b_c"


def test_default_is_login_first_when_no_setting_file():
    accounts.access_file().unlink(missing_ok=True)
    assert accounts.load_access().login_enabled
