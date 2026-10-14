"""Unexpected API and browser errors, one row per fingerprint (reliability plan 2026-10-14).

Revision ID: 0136_app_errors
Revises: 0135_drop_macro_fallback
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0136_app_errors"
down_revision = "0135_drop_macro_fallback"
branch_labels = None
depends_on = None

_TABLE = "app_errors"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("kind", sa.String(120), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("location", sa.String(300), nullable=False),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_app_errors_fingerprint", _TABLE, ["fingerprint"], unique=True)
        op.create_index("ix_app_errors_last_seen", _TABLE, ["last_seen"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    op.drop_index("ix_app_errors_last_seen", table_name=_TABLE)
    op.drop_index("ix_app_errors_fingerprint", table_name=_TABLE)
    op.drop_table(_TABLE)
