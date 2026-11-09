"""The nightly forward-test lab and the overnight news tone scores (plan 2026-11-09).

* ``forward_lab_snapshots``: each signal's scores and weights for every
  universe name, frozen before the next day's prices exist.
* ``forward_lab_daily``: the next-day return of each snapshot's portfolio.
* ``news_tone_scores``: the overnight LLM reading of a stock's headlines.

Revision ID: 0146_forward_lab
Revises: 0145_index_universe
"""
from alembic import op
import sqlalchemy as sa

revision = "0146_forward_lab"
down_revision = "0145_index_universe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("forward_lab_snapshots"):
        op.create_table(
            "forward_lab_snapshots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("as_of", sa.Date(), nullable=False),
            sa.Column("signal", sa.String(48), nullable=False),
            sa.Column("scores_json", sa.JSON(), nullable=False),
            sa.Column("weights_json", sa.JSON(), nullable=False),
            sa.Column("n", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("as_of", "signal", name="uq_forward_lab_snapshot"),
        )
        op.create_index("ix_forward_lab_snapshots_as_of", "forward_lab_snapshots", ["as_of"])
    if not inspector.has_table("forward_lab_daily"):
        op.create_table(
            "forward_lab_daily",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("signal", sa.String(48), nullable=False),
            sa.Column("as_of", sa.Date(), nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("value_rank", sa.Float(), nullable=True),
            sa.Column("value_efficient", sa.Float(), nullable=True),
            sa.Column("rank_ic", sa.Float(), nullable=True),
            sa.Column("n", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("signal", "as_of", name="uq_forward_lab_daily"),
        )
        op.create_index("ix_forward_lab_daily_signal", "forward_lab_daily", ["signal"])
    if not inspector.has_table("news_tone_scores"):
        op.create_table(
            "news_tone_scores",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("tone", sa.Float(), nullable=False),
            sa.Column("n_headlines", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("events_json", sa.JSON(), nullable=False),
            sa.Column("model", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("symbol", "day", name="uq_news_tone_score"),
        )
        op.create_index("ix_news_tone_scores_symbol", "news_tone_scores", ["symbol"])
        op.create_index("ix_news_tone_scores_day", "news_tone_scores", ["day"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("news_tone_scores", "forward_lab_daily", "forward_lab_snapshots"):
        if inspector.has_table(table):
            op.drop_table(table)
