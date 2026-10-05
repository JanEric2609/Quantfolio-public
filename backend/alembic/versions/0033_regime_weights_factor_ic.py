"""Add regime_recommendation_weights and factor_ic_tracking tables.

Revision ID: 0033_regime_weights_factor_ic
Revises: 0032_recommendation_outcomes
Create Date: 2026-06-05
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_regime_weights_factor_ic"
down_revision = "0032_recommendation_outcomes"
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

    if not inspector.has_table("regime_recommendation_weights"):
        op.create_table(
            "regime_recommendation_weights",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("regime_label", sa.String(24), nullable=False, index=True),
            sa.Column("action", sa.String(64), nullable=False, index=True),
            sa.Column("weight", sa.Float, nullable=False, server_default="1.0"),
            sa.Column("n_obs", sa.Integer, nullable=False, server_default="0"),
            sa.Column("cum_accuracy", sa.Float, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("regime_label", "action", name="uq_regime_action"),
        )
        safe_create_index(
            "ix_regime_weights_label_action",
            "regime_recommendation_weights",
            ["regime_label", "action"],
        )

    if not inspector.has_table("factor_ic_tracking"):
        op.create_table(
            "factor_ic_tracking",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("regime_label", sa.String(24), nullable=False, index=True),
            sa.Column("factor_name", sa.String(64), nullable=False, index=True),
            sa.Column("ic_value", sa.Float, nullable=False, server_default="0.0"),
            sa.Column("t_stat", sa.Float, nullable=True),
            sa.Column("n_obs", sa.Integer, nullable=False, server_default="0"),
            sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("notes_json", sa.Text, nullable=True),
        )
        safe_create_index(
            "ix_factor_ic_tracking_regime_factor",
            "factor_ic_tracking",
            ["regime_label", "factor_name"],
        )


def downgrade() -> None:
    op.drop_table("factor_ic_tracking")
    op.drop_table("regime_recommendation_weights")
