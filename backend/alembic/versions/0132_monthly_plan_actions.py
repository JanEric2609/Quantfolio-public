"""One stored row per monthly-plan action, so a placed order links back to it (ADR 0019 §3).

Revision ID: 0132_monthly_plan_actions
Revises: 0131_trust_factor_study
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0132_monthly_plan_actions"
down_revision = "0131_trust_factor_study"
branch_labels = None
depends_on = None

_TABLE = "monthly_plan_actions"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("month", sa.String(7), nullable=False),
            sa.Column("sleeve", sa.String(16), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("amount_eur", sa.Float(), nullable=False, server_default="0"),
            sa.Column("instrument", sa.String(128), nullable=False, server_default=""),
            sa.Column("ticker", sa.String(32), nullable=False, server_default=""),
            sa.Column("isin", sa.String(16), nullable=False, server_default=""),
            sa.Column("broker", sa.String(16), nullable=False, server_default=""),
            sa.Column("account_id", sa.String(36), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="open"),
            sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("detail_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "user_id", "month", "sleeve", "kind", "isin", "ticker", "broker", "account_id",
                name="uq_plan_action_month_item",
            ),
        )
        op.create_index("ix_monthly_plan_actions_user_id", _TABLE, ["user_id"])
        op.create_index("ix_monthly_plan_actions_month", _TABLE, ["month"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    op.drop_index("ix_monthly_plan_actions_month", table_name=_TABLE)
    op.drop_index("ix_monthly_plan_actions_user_id", table_name=_TABLE)
    op.drop_table(_TABLE)
