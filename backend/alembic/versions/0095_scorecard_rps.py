"""Add AdvisorScorecard.rps_avg — bucketed Ranked Probability Score (F11).

composite_score's calibration axis previously used brier_avg (sign-only:
conviction vs whether realised_return > 0), which never checked whether the
predicted *magnitude* distribution (MC p5/p95 band) was calibrated. rps_avg
is a bucketed RPS fallback for a true CRPS -- raw MC paths are discarded
after computing the p5/p50/p95 summary, so a true CRPS isn't buildable
without a separate quant_mc storage change (scope-reduced per user
decision).

Revision ID: 0095_scorecard_rps
Revises: 0094_scorecard_psr_dsr
"""

from alembic import op
import sqlalchemy as sa

revision = "0095_scorecard_rps"
down_revision = "0094_scorecard_psr_dsr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("advisor_scorecards"):
        return
    columns = {col["name"] for col in inspector.get_columns("advisor_scorecards")}
    if "rps_avg" not in columns:
        op.add_column("advisor_scorecards", sa.Column("rps_avg", sa.Float(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("advisor_scorecards"):
        return
    columns = {col["name"] for col in inspector.get_columns("advisor_scorecards")}
    if "rps_avg" in columns:
        op.drop_column("advisor_scorecards", "rps_avg")
