"""Telegram bot integration (Money Phase 3) — telegram_accounts + telegram_pairing_codes.

telegram_accounts links a user to their linked Telegram chat_id.
telegram_pairing_codes holds short-lived one-time linking codes.

Revision ID: 0084_telegram_accounts
Revises: 0083_envelope_budgets
Create Date: 2026-07-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0084_telegram_accounts"
down_revision = "0083_envelope_budgets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("telegram_accounts"):
        op.create_table(
            "telegram_accounts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
                index=True,
            ),
            sa.Column("chat_id", sa.String(64), nullable=False, unique=True, index=True),
            sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        )

    if not inspector.has_table("telegram_pairing_codes"):
        op.create_table(
            "telegram_pairing_codes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
                index=True,
            ),
            sa.Column("code", sa.String(12), nullable=False, unique=True, index=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("telegram_pairing_codes"):
        op.drop_table("telegram_pairing_codes")
    if inspector.has_table("telegram_accounts"):
        op.drop_table("telegram_accounts")
