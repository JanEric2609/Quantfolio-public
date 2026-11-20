"""Add evidence_gate_runs (report Phase 4).

Holds each DSR/PBO grading by ``app.lab.evidence_gate`` of the mined scores'
satellite records. The monthly plan unlocks the stock-picking satellite only
from the latest run's verdict.

Revision ID: 0111_evidence_gate_runs
Revises: 0110_factor_evidence_cards
"""

from alembic import op
import sqlalchemy as sa

revision = "0111_evidence_gate_runs"
down_revision = "0110_factor_evidence_cards"
branch_labels = None
depends_on = None

_TABLE = "evidence_gate_runs"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("satellite_unlocked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("unlocked_by", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("n_trials", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evidence_gate_runs_region", _TABLE, ["region"])
    op.create_index("ix_evidence_gate_runs_computed_at", _TABLE, ["computed_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)
