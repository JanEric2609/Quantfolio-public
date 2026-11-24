"""desk_cfd_journal.market_key wider: the event desk's stock keys such as US:NVDA (ADR 0028).

Revision ID: 0159_cfd_journal_wider
Revises: 0158_desk_cfd_journal
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0159_cfd_journal_wider"
down_revision = "0158_desk_cfd_journal"
branch_labels = None
depends_on = None


def _set_length(length: int) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("desk_cfd_journal"):
        return
    with op.batch_alter_table("desk_cfd_journal") as batch:
        batch.alter_column("market_key", existing_type=sa.String(), type_=sa.String(length), existing_nullable=False)


def upgrade() -> None:
    _set_length(24)


def downgrade() -> None:
    _set_length(8)
