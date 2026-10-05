"""Login lockout keyed on (username, client IP), with a per-username ceiling.

Every client reaches the API through Caddy. Once uvicorn trusts the proxy
(``--proxy-headers``) ``request.client`` is the real address, and the lockout
must hit the attacker's address, not the account's owner.
"""
import base64
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from conftest import _memory_db
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.foundation import auth as auth_module
from app.foundation.auth import (
    LOGIN_MAX_ATTEMPTS,
    LOGIN_USERNAME_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
    LoginTracker,
    authenticate_password,
)
from app.foundation.core.db import get_db
from app.foundation.core.security import client_ip, hash_password, limiter, verify_password
from app.foundation.models.entities import User
from app.main import app


def _make_user(db, username="alice", password="Correct1!"):
    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return user


@pytest.fixture(autouse=True)
def _fresh_state():
    auth_module.login_tracker._attempts.clear()
    limiter.reset()
    yield
    auth_module.login_tracker._attempts.clear()
    limiter.reset()
    app.dependency_overrides.clear()


# -- LoginTracker ------------------------------------------------------------


def test_lockout_is_per_username_and_ip():
    tracker = LoginTracker()
    for _ in range(LOGIN_MAX_ATTEMPTS):
        tracker.record_failure("jan", "203.0.113.9")

    assert tracker.is_blocked("jan", "203.0.113.9")
    # The owner, from home, is not locked out by an attacker elsewhere.
    assert not tracker.is_blocked("jan", "198.51.100.7")


def test_distributed_guessing_hits_the_per_username_ceiling():
    tracker = LoginTracker()
    for i in range(LOGIN_USERNAME_MAX_ATTEMPTS):
        tracker.record_failure("jan", f"10.0.{i // 250}.{i % 250}")

    # Each address failed only once, yet a fresh address is stopped by the username-wide total.
    assert tracker.is_blocked("jan", "192.0.2.200")
    assert not tracker.is_blocked("someone-else", "192.0.2.200")


def test_ceiling_is_far_above_the_pair_limit():
    assert LOGIN_USERNAME_MAX_ATTEMPTS >= 10 * LOGIN_MAX_ATTEMPTS


def test_success_clears_only_that_pair():
    tracker = LoginTracker()
    for _ in range(3):
        tracker.record_failure("jan", "203.0.113.9")
        tracker.record_failure("jan", "198.51.100.7")

    tracker.clear("jan", "198.51.100.7")

    assert tracker._attempts.get(tracker._pair_key("jan", "198.51.100.7")) is None
    assert len(tracker._attempts[tracker._pair_key("jan", "203.0.113.9")]) == 3
    # The username-wide total is left to expire: a login cannot reset an attacker's budget.
    assert len(tracker._attempts["jan"]) == 6


def test_clear_without_an_address_forgets_the_whole_username():
    tracker = LoginTracker()
    tracker.record_failure("jan", "203.0.113.9")
    tracker.record_failure("jan", "198.51.100.7")
    tracker.record_failure("other", "203.0.113.9")

    tracker.clear("jan")

    assert not any(key.split("\x00")[0] == "jan" for key in tracker._attempts)
    assert "other" in tracker._attempts


def test_expired_failures_stop_counting_per_pair():
    tracker = LoginTracker()
    stale = datetime.now(UTC) - timedelta(seconds=LOGIN_WINDOW_SECONDS + 1)
    tracker._attempts[tracker._pair_key("jan", "203.0.113.9")] = [stale] * LOGIN_MAX_ATTEMPTS

    assert not tracker.is_blocked("jan", "203.0.113.9")


def test_sweep_drops_expired_keys_of_made_up_usernames(monkeypatch):
    monkeypatch.setattr(auth_module, "_TRACKER_SWEEP_THRESHOLD", 20)
    tracker = LoginTracker()
    stale = datetime.now(UTC) - timedelta(seconds=LOGIN_WINDOW_SECONDS + 1)
    for i in range(30):
        tracker._attempts[f"ghost{i}"] = [stale]

    tracker.record_failure("jan", "203.0.113.9")

    assert not any(key.startswith("ghost") for key in tracker._attempts)
    assert "jan" in tracker._attempts


# -- authenticate_password ---------------------------------------------------


def test_attacker_address_is_locked_out_but_owner_still_logs_in():
    db = _memory_db()
    _make_user(db, "jan", "Correct1!")
    for _ in range(LOGIN_MAX_ATTEMPTS):
        with pytest.raises(HTTPException) as exc:
            authenticate_password(db, "jan", "wrong", "203.0.113.9")
        assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as blocked:
        authenticate_password(db, "jan", "Correct1!", "203.0.113.9")
    assert blocked.value.status_code == 429

    owner = authenticate_password(db, "jan", "Correct1!", "198.51.100.7")
    assert owner.username == "jan"


def test_unknown_username_costs_a_full_password_hash():
    """No early return for unknown users: a PBKDF2 check runs against a dummy hash."""
    db = _memory_db()
    calls: list[str] = []
    real = auth_module.verify_password

    def spy(password, password_hash):
        calls.append(password_hash)
        return real(password, password_hash)

    with patch.object(auth_module, "verify_password", spy):
        with pytest.raises(HTTPException) as exc:
            authenticate_password(db, "nobody", "whatever", "203.0.113.9")

    assert exc.value.status_code == 401
    assert calls == [auth_module._DUMMY_PASSWORD_HASH]


def test_dummy_hash_is_well_formed_so_it_really_runs_pbkdf2():
    scheme, salt_b64, digest_b64 = auth_module._DUMMY_PASSWORD_HASH.split("$", 2)
    assert scheme == "pbkdf2_sha256"
    assert len(base64.b64decode(salt_b64)) == 16
    assert len(base64.b64decode(digest_b64)) == 32
    assert verify_password("anything", auth_module._DUMMY_PASSWORD_HASH) is False


def test_unknown_and_wrong_password_answer_identically():
    db = _memory_db()
    _make_user(db, "jan", "Correct1!")
    with pytest.raises(HTTPException) as unknown:
        authenticate_password(db, "nobody", "x", "203.0.113.9")
    with pytest.raises(HTTPException) as wrong:
        authenticate_password(db, "jan", "x", "203.0.113.10")
    assert (unknown.value.status_code, unknown.value.detail) == (wrong.value.status_code, wrong.value.detail)


# -- through the HTTP layer --------------------------------------------------


def _client(db, host: str) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app, client=(host, 50000))


def test_login_endpoint_locks_out_the_attacker_not_the_owner():
    db = _memory_db()
    _make_user(db, "jan", "Correct1!")
    attacker = _client(db, "203.0.113.9")
    owner = _client(db, "198.51.100.7")

    for _ in range(LOGIN_MAX_ATTEMPTS):
        assert attacker.post("/api/auth/login", json={"username": "jan", "password": "wrong-pw!"}).status_code == 401
    assert attacker.post("/api/auth/login", json={"username": "jan", "password": "Correct1!"}).status_code == 429

    assert owner.post("/api/auth/login", json={"username": "jan", "password": "Correct1!"}).status_code == 200


def test_forwarded_for_header_is_not_trusted_by_the_app_itself():
    """Only uvicorn's --forwarded-allow-ips may rewrite the client; the app never reads the header."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"9.9.9.9")],
        "client": ("10.0.0.23", 41000),
    }
    assert client_ip(Request(scope)) == "10.0.0.23"
    assert client_ip(Request({"type": "http", "headers": [], "client": None})) == "unknown"


# -- passkey options ---------------------------------------------------------


def test_passkey_options_do_not_enumerate_usernames():
    from app.foundation.models.entities import PasskeyCredential

    db = _memory_db()
    user = _make_user(db, "jan")
    db.add(PasskeyCredential(user_id=user.id, credential_id="cred-abc", public_key="pk"))
    db.commit()
    client = _client(db, "203.0.113.9")

    known = client.post("/api/auth/passkey/authenticate/options", json={"username": "jan"})
    unknown = client.post("/api/auth/passkey/authenticate/options", json={"username": "nobody"})
    anonymous = client.post("/api/auth/passkey/authenticate/options", json={})

    assert known.status_code == unknown.status_code == anonymous.status_code == 200
    assert set(known.json()) == set(unknown.json()) == set(anonymous.json())
    assert known.json().get("allowCredentials", []) == unknown.json().get("allowCredentials", []) == []
    assert "cred-abc" not in known.text


def test_passkey_options_challenge_still_binds_the_named_account():
    db = _memory_db()
    user = _make_user(db, "jan")
    client = _client(db, "203.0.113.9")

    challenge = client.post("/api/auth/passkey/authenticate/options", json={"username": "jan"}).json()["challenge"]
    stored = auth_module.challenge_store.pop(challenge)

    assert stored == {"kind": "authenticate", "user_id": user.id}


def test_passkey_options_are_rate_limited():
    db = _memory_db()
    client = _client(db, "203.0.113.9")

    statuses = [
        client.post("/api/auth/passkey/authenticate/options", json={}).status_code for _ in range(25)
    ]

    assert statuses[:20] == [200] * 20
    assert 429 in statuses[20:]
