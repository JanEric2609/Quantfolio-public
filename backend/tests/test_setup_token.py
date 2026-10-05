"""First-run registration needs the one-time setup token.

A fresh or wiped instance belongs to whoever registers first, so the first
account must present a token only the operator can see (``SETUP_TOKEN`` or a
generated 0600 file in the data dir), the has-users check and insert must be one
step, and the first user is the admin.
"""
import logging
import stat
import threading

import pytest
from conftest import _memory_db
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.foundation import setup_token
from app.foundation.auth import has_users, register_first_user
from app.foundation.core.config import get_settings
from app.foundation.core.db import Base, get_db
from app.foundation.core.security import limiter
from app.foundation.models.entities import ApiKey, Portfolio, User
from app.foundation.setup_token import (
    TOKEN_FILENAME,
    announce_setup_token_if_needed,
    discard_setup_token,
    get_or_create_setup_token,
    setup_token_path,
    verify_setup_token,
)
from app.main import app

ENV_TOKEN = "test-setup-token"  # tests/conftest.py


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    limiter.reset()
    setup_token._ephemeral_token = None
    yield
    app.dependency_overrides.clear()
    limiter.reset()
    setup_token._ephemeral_token = None


@pytest.fixture
def generated_token_mode(monkeypatch, tmp_path):
    """No SETUP_TOKEN in the environment: the token is generated into DATA_DIR."""
    monkeypatch.delenv("SETUP_TOKEN", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _client(db) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _register(client, token=None, username="jan", password="Sup3rSecret!"):
    body = {"username": username, "password": password}
    if token is not None:
        body["setup_token"] = token
    return client.post("/api/auth/register", json=body)


# -- the token itself --------------------------------------------------------


def test_env_token_is_used_and_compared(monkeypatch):
    assert get_or_create_setup_token() == ENV_TOKEN
    assert verify_setup_token(ENV_TOKEN)
    assert verify_setup_token(f"  {ENV_TOKEN}  ")
    assert not verify_setup_token("wrong")
    assert not verify_setup_token("")
    assert not verify_setup_token(None)


def test_token_comparison_is_constant_time(monkeypatch):
    seen = []
    real = setup_token.hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(setup_token.hmac, "compare_digest", spy)
    assert not verify_setup_token("nope")
    assert seen == [(b"nope", ENV_TOKEN.encode())]


def test_generated_token_is_written_0600_and_stable(generated_token_mode):
    first = get_or_create_setup_token()
    path = setup_token_path()

    assert path == generated_token_mode / TOKEN_FILENAME
    assert path.read_text().strip() == first
    assert len(first) >= 24
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A restart (new process state) reads the same token back.
    setup_token._ephemeral_token = None
    assert get_or_create_setup_token() == first
    assert verify_setup_token(first)
    assert not verify_setup_token(first + "x")


def test_startup_logs_the_generated_token_at_warning(generated_token_mode, caplog):
    with caplog.at_level(logging.WARNING, logger="app.foundation.setup_token"):
        announce_setup_token_if_needed(has_users=False)

    token = get_or_create_setup_token()
    record = next(r for r in caplog.records if "FIRST-RUN SETUP" in r.getMessage())
    assert record.levelno == logging.WARNING
    assert token in record.getMessage()
    assert str(setup_token_path()) in record.getMessage()


def test_env_token_is_not_logged_or_written(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="app.foundation.setup_token"):
            announce_setup_token_if_needed(has_users=False)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    assert any("FIRST-RUN SETUP" in r.getMessage() for r in caplog.records)
    assert ENV_TOKEN not in caplog.text
    assert not (tmp_path / TOKEN_FILENAME).exists()


def test_token_file_is_removed_once_users_exist(generated_token_mode):
    get_or_create_setup_token()
    assert setup_token_path().exists()

    announce_setup_token_if_needed(has_users=True)

    assert not setup_token_path().exists()


def test_unwritable_data_dir_falls_back_to_an_in_memory_token(monkeypatch, tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.delenv("SETUP_TOKEN", raising=False)
    monkeypatch.setenv("DATA_DIR", str(blocker / "sub"))
    get_settings.cache_clear()
    try:
        token = get_or_create_setup_token()
        assert get_or_create_setup_token() == token
        assert verify_setup_token(token)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# -- registration ------------------------------------------------------------


def test_register_without_a_token_is_refused():
    db = _memory_db()
    client = _client(db)

    for response in (_register(client), _register(client, token="")):
        assert response.status_code == 403
        assert "setup token" in response.json()["error"]["message"].lower()
    assert not has_users(db)


def test_register_with_a_wrong_token_is_refused():
    db = _memory_db()

    response = _register(_client(db), token="not-the-token")

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Invalid setup token"
    assert not has_users(db)


def test_register_with_the_right_token_creates_an_admin_and_signs_in():
    db = _memory_db()

    response = _register(_client(db), token=ENV_TOKEN)

    assert response.status_code == 200
    assert "quantfolio_session" in response.headers.get("set-cookie", "")
    user = db.query(User).one()
    assert user.username == "jan"
    assert user.role == "admin"
    assert db.query(Portfolio).filter(Portfolio.user_id == user.id).count() == 1


def test_second_registration_is_still_409_even_with_the_token():
    db = _memory_db()
    client = _client(db)
    assert _register(client, token=ENV_TOKEN).status_code == 200

    again = _register(client, token=ENV_TOKEN, username="mallory")
    no_token = _register(client, username="mallory")

    assert again.status_code == 409
    assert no_token.status_code == 409
    assert db.query(User).count() == 1


def test_generated_token_is_consumed_by_the_first_registration(generated_token_mode):
    token = get_or_create_setup_token()
    db = _memory_db()
    client = _client(db)

    assert _register(client, token="guess").status_code == 403
    assert setup_token_path().exists()
    assert _register(client, token=token).status_code == 200

    assert not setup_token_path().exists()


def test_register_first_user_requires_the_token_argument():
    db = _memory_db()
    with pytest.raises(HTTPException) as exc:
        register_first_user(db, "jan", "Sup3rSecret!", None)
    assert exc.value.status_code == 403


def test_concurrent_first_registrations_admit_exactly_one(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    barrier = threading.Barrier(4)
    outcomes: list[object] = []

    def attempt(n: int) -> None:
        db = factory()
        try:
            barrier.wait()
            register_first_user(db, f"user{n}", "Sup3rSecret!", ENV_TOKEN)
            outcomes.append("created")
        except HTTPException as exc:
            outcomes.append(exc.status_code)
        finally:
            db.close()

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(map(str, outcomes)) == ["409", "409", "409", "created"]
    with factory() as db:
        assert db.query(User).count() == 1
        assert db.query(Portfolio).count() == 1


# -- status endpoints leak nothing -------------------------------------------


def test_setup_status_says_only_setup_required_on_a_fresh_instance():
    db = _memory_db()
    # Credentials left over from a previous life of the database must not show.
    db.add(ApiKey(service="finnhub", key_encrypted="x", meta_json="{}"))
    db.commit()

    body = _client(db).get("/api/setup/status").json()

    assert body == {"initialized": False, "has_passkey": False, "integrations": {}, "next_step": "account"}


def test_setup_status_after_initialisation_is_bare_for_anonymous_callers():
    db = _memory_db()
    client = _client(db)
    assert _register(client, token=ENV_TOKEN).status_code == 200

    anonymous = TestClient(app).get("/api/setup/status").json()

    assert anonymous == {"initialized": True, "has_passkey": False, "integrations": {}, "next_step": ""}
    signed_in = client.get("/api/setup/status").json()
    assert signed_in["initialized"] is True
    assert signed_in["next_step"] == "passkey"
    assert "llm" in signed_in["integrations"]


def test_auth_status_only_reports_initialised():
    db = _memory_db()
    assert _client(db).get("/api/auth/status").json() == {"initialized": False}


def test_discard_is_safe_without_a_token_file(generated_token_mode):
    discard_setup_token()
    discard_setup_token()
