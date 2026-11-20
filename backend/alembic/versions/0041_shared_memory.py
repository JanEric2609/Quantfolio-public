"""Add shared_memory to alphacrafter_job_runs and regime_applicability to factors_library.

Revision ID: 0041_shared_memory
Revises: 0040_recommendation_engine
Create Date: 2026-06-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_shared_memory"
down_revision = "0040_recommendation_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("alphacrafter_job_runs"):
        existing_cols = {c["name"] for c in inspector.get_columns("alphacrafter_job_runs")}
        if "shared_memory" not in existing_cols:
            op.add_column(
                "alphacrafter_job_runs",
                sa.Column("shared_memory", sa.JSON, nullable=True),
            )

    if inspector.has_table("factors_library"):
        existing_cols = {c["name"] for c in inspector.get_columns("factors_library")}
        if "regime_applicability" not in existing_cols:
            op.add_column(
                "factors_library",
                sa.Column("regime_applicability", sa.JSON, nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("factors_library"):
        existing_cols = {c["name"] for c in inspector.get_columns("factors_library")}
        if "regime_applicability" in existing_cols:
            op.drop_column("factors_library", "regime_applicability")

    if inspector.has_table("alphacrafter_job_runs"):
        existing_cols = {c["name"] for c in inspector.get_columns("alphacrafter_job_runs")}
        if "shared_memory" in existing_cols:
            op.drop_column("alphacrafter_job_runs", "shared_memory")
