"""Add outcome-scoring columns to llm_portfolio_decisions.

The mandate-review redesign scores each review's structured expectation once its
horizon elapses. Three cheap SQL-aggregatable columns back that loop:
  * verdict       — hit / miss / partial (nullable until scored)
  * horizon_weeks — measurement window from the decision's expectation
  * scored_at     — when the outcome was computed

Prose (assessment facts, alternatives, expectation, actual outcome) lives in the
existing decision_json / reflection_json blobs.

Revision ID: 0078_llm_decision_verdict
Revises: 0077_fx_rates
"""
from alembic import op
import sqlalchemy as sa

revision = "0078_llm_decision_verdict"
down_revision = "0077_fx_rates"
branch_labels = None
depends_on = None

_TABLE = "llm_portfolio_decisions"


def _columns(inspector) -> set[str]:
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    cols = _columns(inspector)
    if "verdict" not in cols:
        op.add_column(_TABLE, sa.Column("verdict", sa.String(length=16), nullable=True))
    if "horizon_weeks" not in cols:
        op.add_column(_TABLE, sa.Column("horizon_weeks", sa.Integer(), nullable=True))
    if "scored_at" not in cols:
        op.add_column(_TABLE, sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    cols = _columns(inspector)
    if "scored_at" in cols:
        op.drop_column(_TABLE, "scored_at")
    if "horizon_weeks" in cols:
        op.drop_column(_TABLE, "horizon_weeks")
    if "verdict" in cols:
        op.drop_column(_TABLE, "verdict")
