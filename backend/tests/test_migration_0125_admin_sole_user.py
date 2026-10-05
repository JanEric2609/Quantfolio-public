"""Migration 0125: the only account becomes admin; several accounts are left alone."""
from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.pool import StaticPool

_PREVIOUS = "0124_trust_ledgers"
_REVISION = "0125_admin_sole_user"


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def test_revision_chain_and_length():
    text = Path("alembic/versions/0125_admin_sole_user.py").read_text()
    assert f'revision = "{_REVISION}"' in text
    assert f'down_revision = "{_PREVIOUS}"' in text
    assert len(_REVISION) <= 32
    assert 'has_table("users")' in text


def _insert_user(conn: sa.Connection, user_id: str, role: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO users (id, username, password_hash, session_version, created_at, role, risk_profile) "
            "VALUES (:id, :name, 'x', 0, '2026-01-01 00:00:00', :role, 'balanced')"
        ),
        {"id": user_id, "name": f"user-{user_id}", "role": role},
    )


@pytest.mark.parametrize(
    "existing,expected",
    [
        ([("u1", "user")], {"u1": "admin"}),
        ([("u1", "user"), ("u2", "user")], {"u1": "user", "u2": "user"}),
        ([("u1", "admin")], {"u1": "admin"}),
        ([], {}),
    ],
)
def test_only_a_sole_account_without_an_admin_is_promoted(existing, expected):
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    cfg = _config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, _PREVIOUS)
        for user_id, role in existing:
            _insert_user(conn, user_id, role)
        command.upgrade(cfg, _REVISION)
        roles = dict(conn.execute(sa.text("SELECT id, role FROM users")).all())
        assert roles == expected
        command.downgrade(cfg, _PREVIOUS)
        assert dict(conn.execute(sa.text("SELECT id, role FROM users")).all()) == expected
