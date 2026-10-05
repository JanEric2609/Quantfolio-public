"""Add competition_runs and competition_decisions tables.

Revision ID: 0061_competition_tables
Revises: 0060_paper_baseline_value
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0061_competition_tables"
down_revision = "0060_paper_baseline_value"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("competition_runs"):
        op.create_table(
            "competition_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "portfolio_a_id",
                sa.String(36),
                sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "portfolio_b_id",
                sa.String(36),
                sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(120), default="Dual Competition"),
            sa.Column("cadence_days", sa.Integer, default=3),
            sa.Column("current_round", sa.Integer, default=1),
            sa.Column("status", sa.String(24), default="init"),
            sa.Column("state_json", sa.Text, default="{}"),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )

    if not inspector.has_table("competition_decisions"):
        op.create_table(
            "competition_decisions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "run_id",
                sa.String(36),
                sa.ForeignKey("competition_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("round_number", sa.Integer, index=True, nullable=False),
            sa.Column(
                "portfolio_id",
                sa.String(36),
                sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("council_result_json", sa.Text, default="{}"),
            sa.Column("debate_report_json", sa.Text, nullable=True),
            sa.Column("blm_results_json", sa.Text, nullable=True),
            sa.Column("executed_trades_json", sa.Text, nullable=True),
            sa.Column("score_json", sa.Text, nullable=True),
            sa.Column("winner", sa.Boolean, default=False),
            sa.Column("errors", sa.Text, default="[]"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("competition_decisions"):
        op.drop_table("competition_decisions")

    if inspector.has_table("competition_runs"):
        op.drop_table("competition_runs")
