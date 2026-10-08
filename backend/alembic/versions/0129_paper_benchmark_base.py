"""Paper portfolios: the benchmark's EUR price at seed time.

The passive benchmark used the last close on or before the inception date
while the sleeve is seeded at the live quote, so the two starts could differ
by a session. These two columns hold the benchmark's quote at the seed moment.

Revision ID: 0129_paper_benchmark_base
Revises: 0128_paper_reset_archive
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0129_paper_benchmark_base"
down_revision = "0128_paper_reset_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("paper_portfolios"):
        return
    columns = {c["name"] for c in inspector.get_columns("paper_portfolios")}
    if "benchmark_base_price" not in columns:
        op.add_column("paper_portfolios", sa.Column("benchmark_base_price", sa.Numeric(20, 8), nullable=True))
    if "benchmark_base_at" not in columns:
        op.add_column(
            "paper_portfolios", sa.Column("benchmark_base_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("paper_portfolios"):
        return
    columns = {c["name"] for c in inspector.get_columns("paper_portfolios")}
    with op.batch_alter_table("paper_portfolios") as batch:
        if "benchmark_base_at" in columns:
            batch.drop_column("benchmark_base_at")
        if "benchmark_base_price" in columns:
            batch.drop_column("benchmark_base_price")
