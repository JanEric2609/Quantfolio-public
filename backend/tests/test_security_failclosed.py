"""Fail-closed production-safety guards (issue #129).

Two defects hardened here:
  1. app_env fail-open: a misspelled/unset APP_ENV must not be treated as local.
  2. JWT->Fernet coupling: the dev-only fallback must never run against a real
     (Postgres) deployment, and its derivation must be domain-separated (HKDF),
     not a raw SHA-256 of the JWT secret.
"""
import base64
import hashlib

import pytest

from app.foundation.core.config import Settings
from app.foundation.core.security import SecretBox
from app.main import _startup_security_checks

_DEFAULT_JWT = "local-dev-change-me"
_PG_DSN = "postgresql+psycopg://u:p@db/quantfolio"
_SQLITE_DSN = "sqlite:///./quantfolio.db"


# --- Problem 1: strict is_local -------------------------------------------

def test_is_local_true_only_for_literal_local():
    assert Settings(app_env="local").is_local is True
    assert Settings(app_env="LOCAL").is_local is True  # case-insensitive
    assert Settings(app_env=" local ").is_local is True  # trimmed


@pytest.mark.parametrize("value", ["", "production", "prod", "loca", "localhost", "dev"])
def test_is_local_false_for_anything_else(value):
    assert Settings(app_env=value).is_local is False


# --- Problem 1+2: Postgres DSN is a production signal regardless of APP_ENV -

def test_startup_rejects_postgres_with_default_jwt_even_when_app_env_local():
    settings = Settings(app_env="local", database_url=_PG_DSN, jwt_secret=_DEFAULT_JWT, encryption_key="k" * 44)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _startup_security_checks(settings)


def test_startup_rejects_postgres_without_encryption_key_even_when_app_env_local():
    settings = Settings(app_env="local", database_url=_PG_DSN, jwt_secret="a-strong-unique-secret", encryption_key=None)
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        _startup_security_checks(settings)


def test_startup_allows_local_sqlite_with_defaults():
    # Dev ergonomics: local sqlite with defaults must remain frictionless.
    settings = Settings(app_env="local", database_url=_SQLITE_DSN, jwt_secret=_DEFAULT_JWT, encryption_key=None)
    _startup_security_checks(settings)  # no raise


def test_secretbox_refuses_jwt_derivation_against_postgres():
    # The weak JWT-derived key must never be used with a real database.
    settings = Settings(app_env="local", database_url=_PG_DSN, jwt_secret="strong-secret", encryption_key=None)
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        SecretBox.from_settings(settings)


# --- Problem 2: HKDF domain separation ------------------------------------

def test_dev_fallback_key_is_not_raw_sha256_of_jwt():
    settings = Settings(app_env="local", database_url=_SQLITE_DSN, jwt_secret="some-dev-secret", encryption_key=None)
    box = SecretBox.from_settings(settings)

    # Old, insecure derivation that this issue removes.
    raw_sha = base64.urlsafe_b64encode(hashlib.sha256(b"some-dev-secret").digest())
    assert box.fernet._signing_key + box.fernet._encryption_key != base64.urlsafe_b64decode(raw_sha)

    # Still a working key: round-trips.
    assert box.decrypt(box.encrypt("dkb-pin")) == "dkb-pin"


def test_dev_fallback_is_deterministic_for_same_secret():
    s = Settings(app_env="local", database_url=_SQLITE_DSN, jwt_secret="same", encryption_key=None)
    a = SecretBox.from_settings(s)
    b = SecretBox.from_settings(s)
    assert b.decrypt(a.encrypt("x")) == "x"


def test_explicit_encryption_key_used_regardless_of_dsn():
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    settings = Settings(app_env="production", database_url=_PG_DSN, jwt_secret="strong", encryption_key=key)
    box = SecretBox.from_settings(settings)
    assert box.decrypt(box.encrypt("secret")) == "secret"


def test_startup_rejects_the_env_example_jwt_placeholder():
    # .env.example ships this value; copying the file as-is must not boot a deployment.
    settings = Settings(app_env="production", database_url=_PG_DSN, jwt_secret="change-this-before-use", encryption_key="k" * 44)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _startup_security_checks(settings)


def test_startup_warns_on_short_jwt_secret(caplog):
    settings = Settings(app_env="production", database_url=_PG_DSN, jwt_secret="short-but-unique", encryption_key="k" * 44)
    _startup_security_checks(settings)
    assert "shorter than 32" in caplog.text
