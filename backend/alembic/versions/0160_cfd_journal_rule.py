"""desk_cfd_journal.rule: which rule the owner's trade followed (ADR 0027 amendment 2026-11-25).

Revision ID: 0160_cfd_journal_rule
Revises: 0159_cfd_journal_wider
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0160_cfd_journal_rule"
down_revision = "0159_cfd_journal_wider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("desk_cfd_journal"):
        return
    if "rule" not in {c["name"] for c in inspector.get_columns("desk_cfd_journal")}:
        op.add_column("desk_cfd_journal",
                      sa.Column("rule", sa.String(12), nullable=False, server_default="registered"))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("desk_cfd_journal") and "rule" in {
            c["name"] for c in inspector.get_columns("desk_cfd_journal")}:
        with op.batch_alter_table("desk_cfd_journal") as batch:
            batch.drop_column("rule")
