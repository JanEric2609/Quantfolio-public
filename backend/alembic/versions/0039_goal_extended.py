"""Extend goals table with risk_tolerance, asset_class_targets, monthly_contribution.

Revision ID: 0039_goal_extended
Revises: 0038_position_snapshot
Create Date: 2026-06-06

Notes
-----
0027_review_repairs uses batch_alter_table(goals, recreate="always") on SQLite,
which pulls in model metadata columns (risk_tolerance etc.) before this migration
runs.  Per-column guards ensure idempotency on SQLite.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_goal_extended"
down_revision = "0038_position_snapshot"
branch_labels = None
depends_on = None


def _column_exists(inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("goals"):
        with op.batch_alter_table("goals") as batch_op:
            if not _column_exists(inspector, "goals", "risk_tolerance"):
                batch_op.add_column(sa.Column("risk_tolerance", sa.String(length=24), nullable=True))
            if not _column_exists(inspector, "goals", "asset_class_targets"):
                batch_op.add_column(sa.Column("asset_class_targets", sa.Text(), nullable=True))
            if not _column_exists(inspector, "goals", "monthly_contribution"):
                batch_op.add_column(sa.Column("monthly_contribution", sa.Numeric(20, 6), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("goals"):
        with op.batch_alter_table("goals") as batch_op:
            if _column_exists(inspector, "goals", "monthly_contribution"):
                batch_op.drop_column("monthly_contribution")
            if _column_exists(inspector, "goals", "asset_class_targets"):
                batch_op.drop_column("asset_class_targets")
            if _column_exists(inspector, "goals", "risk_tolerance"):
                batch_op.drop_column("risk_tolerance")
