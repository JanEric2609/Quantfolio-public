"""Startup warning for localhost webauthn_rp_id behind a remote origin (audit §8).

WebAuthn credentials are bound to the RP ID: if rp_id stays 'localhost' while
the app is served from e.g. https://x.ts.net, every passkey registration and
authentication fails. The startup check is pure-warning: it never mutates
settings and never raises.
"""
import logging

from conftest import _memory_db

from app.foundation.core.config import Settings
from app.main import _check_webauthn_rp_id
from app.foundation.settings import upsert_public_settings


def _settings(**overrides) -> Settings:
    return Settings(jwt_secret="test-secret", encryption_key=None, **overrides)


def test_webauthn_warning_fires_when_localhost_rp_id_behind_remote_origin(caplog):
    db = _memory_db()
    upsert_public_settings(db, {"frontend_origin": "https://x.ts.net"})

    with caplog.at_level(logging.WARNING, logger="quantfolio"):
        _check_webauthn_rp_id(db, _settings(webauthn_rp_id="localhost"))  # no raise

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Passkeys will FAIL" in warnings[0].getMessage()
    assert "https://x.ts.net" in warnings[0].getMessage()


def test_no_webauthn_warning_when_rp_id_matches_origin_host(caplog):
    db = _memory_db()
    upsert_public_settings(
        db,
        {
            "webauthn_rp_id": "quantfolio-web.tailnet-example.ts.net",
            "frontend_origin": "https://quantfolio-web.tailnet-example.ts.net",
        },
    )

    with caplog.at_level(logging.DEBUG, logger="quantfolio"):
        _check_webauthn_rp_id(db, _settings())

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_webauthn_warning_for_localhost_origin_with_default_rp_id(caplog):
    # Default local-dev shape: rp_id=localhost + http://localhost:5173 origin.
    db = _memory_db()

    with caplog.at_level(logging.DEBUG, logger="quantfolio"):
        _check_webauthn_rp_id(db, _settings(webauthn_rp_id="localhost"))

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_missing_origin_degrades_webauthn_warning_to_debug_without_raising(caplog):
    # Whitespace-only CSV survives resolve_setting's truthiness check but
    # resolves to ZERO origins after split+strip+filter — the "missing origin"
    # shape. (An empty-string row falls back to the built-in default instead.)
    db = _memory_db()
    upsert_public_settings(db, {"frontend_origin": "   "})

    with caplog.at_level(logging.DEBUG, logger="quantfolio"):
        _check_webauthn_rp_id(db, _settings(webauthn_rp_id="localhost"))  # no raise

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("frontend_origin" in r.getMessage() for r in debugs)


def test_unparseable_origin_degrades_webauthn_warning_to_debug_without_raising(caplog):
    db = _memory_db()
    upsert_public_settings(db, {"frontend_origin": "not-a-valid-origin"})

    with caplog.at_level(logging.DEBUG, logger="quantfolio"):
        _check_webauthn_rp_id(db, _settings(webauthn_rp_id="localhost"))  # no raise

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
