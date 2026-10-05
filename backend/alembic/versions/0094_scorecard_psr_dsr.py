"""Add AdvisorScorecard.psr/dsr — Probabilistic/Deflated Sharpe Ratio.

composite_score's risk_adjusted axis previously used sigmoid(sharpe), which
treats a short, noisy window's raw Sharpe as if it were already skill-
adjusted. PSR/DSR (Bailey & Lopez de Prado) account for sample size, skew,
kurtosis, and (for DSR) the number of strategy variants trialed.

Revision ID: 0094_scorecard_psr_dsr
Revises: 0093_discover_run_updated_at
"""

from alembic import op
import sqlalchemy as sa

revision = "0094_scorecard_psr_dsr"
down_revision = "0093_discover_run_updated_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("advisor_scorecards"):
        return
    columns = {col["name"] for col in inspector.get_columns("advisor_scorecards")}
    if "psr" not in columns:
        op.add_column("advisor_scorecards", sa.Column("psr", sa.Float(), nullable=True))
    if "dsr" not in columns:
        op.add_column("advisor_scorecards", sa.Column("dsr", sa.Float(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("advisor_scorecards"):
        return
    columns = {col["name"] for col in inspector.get_columns("advisor_scorecards")}
    if "dsr" in columns:
        op.drop_column("advisor_scorecards", "dsr")
    if "psr" in columns:
        op.drop_column("advisor_scorecards", "psr")
