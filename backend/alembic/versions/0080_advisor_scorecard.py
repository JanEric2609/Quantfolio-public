"""Add advisor_scorecards table — per-cohort 4-axis improvement scorecard.

Advisor-loop PR1 (E1): risk-adjusted return, calibration, magnitude accuracy
(Mincer-Zarnowitz), and downside discipline, keyed by (portfolio_id,
window_end). Additive only — no column drops.

Revision ID: 0080_advisor_scorecard
Revises: 0079_drop_plan_knowledge
"""

from alembic import op
import sqlalchemy as sa

revision = "0080_advisor_scorecard"
down_revision = "0079_drop_plan_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("advisor_scorecards"):
        op.create_table(
            "advisor_scorecards",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "portfolio_id",
                sa.String(36),
                sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("window_start", sa.Date(), nullable=False),
            sa.Column("window_end", sa.Date(), nullable=False, index=True),
            sa.Column("n_predictions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("n_resolved", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sharpe", sa.Float(), nullable=True),
            sa.Column("sortino", sa.Float(), nullable=True),
            sa.Column("calmar", sa.Float(), nullable=True),
            sa.Column("brier_avg", sa.Float(), nullable=True),
            sa.Column("log_loss_avg", sa.Float(), nullable=True),
            sa.Column("mz_slope", sa.Float(), nullable=True),
            sa.Column("mz_r2", sa.Float(), nullable=True),
            sa.Column("max_drawdown", sa.Float(), nullable=True),
            sa.Column("cvar_95", sa.Float(), nullable=True),
            sa.Column("details_json", sa.JSON(), nullable=False),
            sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("portfolio_id", "window_end", name="uq_advisor_scorecard_window"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("advisor_scorecards"):
        op.drop_table("advisor_scorecards")
