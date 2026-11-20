"""Add confidence_scores and verification_alerts tables.

Revision ID: 0042_verification_rework
Revises: 0041_shared_memory
Create Date: 2026-06-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_verification_rework"
down_revision = "0041_shared_memory"
branch_labels = None
depends_on = None


def _index_names(table_name):
    """Return the set of existing index names on ``table_name`` (empty if the table is gone)."""
    inspector = sa.inspect(op.get_bind())
    try:
        return {idx["name"] for idx in inspector.get_indexes(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def safe_create_index(index_name, table_name, columns, unique=False, **kwargs):
    """Create an index only if it does not already exist (idempotent / duplicate-safe)."""
    resolved = op.f(index_name)
    if resolved not in _index_names(table_name):
        op.create_index(resolved, table_name, columns, unique=unique, **kwargs)


def safe_drop_index(index_name, table_name=None, **kwargs):
    """Drop an index only if it exists (idempotent / safe on re-run)."""
    resolved = op.f(index_name)
    if table_name is not None and resolved in _index_names(table_name):
        op.drop_index(resolved, table_name=table_name, **kwargs)
    elif table_name is None:
        op.drop_index(resolved, **kwargs)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("confidence_scores"):
        op.create_table(
            "confidence_scores",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "portfolio_id",
                sa.String(),
                sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("overall", sa.Numeric(6, 4), nullable=False),
            sa.Column("historical_performance", sa.Numeric(6, 4), nullable=False),
            sa.Column("live_tracking", sa.Numeric(6, 4), nullable=False),
            sa.Column("risk_profile", sa.Numeric(6, 4), nullable=False),
            sa.Column("regime_adaptability", sa.Numeric(6, 4), nullable=False),
            sa.Column("factors_json", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        safe_create_index(
            "ix_confidence_scores_portfolio_created",
            "confidence_scores",
            ["portfolio_id", "created_at"],
        )

    if not inspector.has_table("verification_alerts"):
        op.create_table(
            "verification_alerts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "portfolio_id",
                sa.String(),
                sa.ForeignKey("portfolios.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("alert_type", sa.String(40), nullable=False),
            sa.Column("severity", sa.String(24), nullable=False),
            sa.Column("title", sa.String(220), nullable=False),
            sa.Column("message", sa.Text, nullable=False),
            sa.Column("action_url", sa.Text, nullable=True),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        safe_create_index(
            "ix_verification_alerts_portfolio_id",
            "verification_alerts",
            ["portfolio_id"],
        )
        safe_create_index(
            "ix_verification_alerts_created_at",
            "verification_alerts",
            ["created_at"],
        )
        safe_create_index(
            "ix_verification_alerts_user_acknowledged",
            "verification_alerts",
            ["user_id", "acknowledged_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("verification_alerts"):
        op.drop_table("verification_alerts")
    if inspector.has_table("confidence_scores"):
        op.drop_table("confidence_scores")
