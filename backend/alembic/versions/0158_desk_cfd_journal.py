"""desk_cfd_journal: the owner's own record of the manual Trading 212 CFD trades (ADR 0027 amendment).

Owner input outside the hash-chained ledger; it never changes a paper position.

Revision ID: 0158_desk_cfd_journal
Revises: 0157_derivatives_desk
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0158_desk_cfd_journal"
down_revision = "0157_derivatives_desk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("desk_cfd_journal"):
        return
    op.create_table(
        "desk_cfd_journal",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("position_id", sa.String(36), sa.ForeignKey("desk_positions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_key", sa.String(8), nullable=False),
        sa.Column("direction", sa.String(5), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("r1", sa.Numeric(14, 8), nullable=True),
        sa.Column("index_level_est", sa.Numeric(20, 6), nullable=True),
        sa.Column("eur_per_unit", sa.Numeric(20, 10), nullable=True),
        sa.Column("quantity_suggested", sa.Numeric(20, 8), nullable=True),
        sa.Column("exposure_eur", sa.Numeric(14, 4), nullable=True),
        sa.Column("stop_points", sa.Numeric(20, 6), nullable=True),
        sa.Column("typical_30m_points", sa.Numeric(20, 6), nullable=True),
        sa.Column("spread_points", sa.Numeric(20, 6), nullable=True),
        sa.Column("enter_by", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="sent"),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_level", sa.Numeric(20, 6), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_level", sa.Numeric(20, 6), nullable=True),
        sa.Column("real_pnl_eur", sa.Numeric(14, 4), nullable=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("last_text_hash", sa.String(64), nullable=True),
    )
    op.create_index("ix_desk_cfd_journal_position_id", "desk_cfd_journal", ["position_id"], unique=True)
    op.create_index("ix_desk_cfd_journal_user_id", "desk_cfd_journal", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("desk_cfd_journal"):
        op.drop_table("desk_cfd_journal")
