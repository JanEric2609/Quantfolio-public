"""Add factor_evidence_cards (report Phase 3).

Holds the prior-informed test result of each pre-registered factor strategy
(value, momentum, ...) computed on the JKP panel by ``app.lab.factor_premia``.
The monthly plan unlocks the factor tilt only from a passing card.

Revision ID: 0110_factor_evidence_cards
Revises: 0109_prediction_excess_return
"""

from alembic import op
import sqlalchemy as sa

revision = "0110_factor_evidence_cards"
down_revision = "0109_prediction_excess_return"
branch_labels = None
depends_on = None

_TABLE = "factor_evidence_cards"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("strategy", sa.String(32), nullable=False),
        sa.Column("region", sa.String(32), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("card_json", sa.JSON(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_factor_evidence_cards_run_id", _TABLE, ["run_id"])
    op.create_index("ix_factor_evidence_cards_strategy", _TABLE, ["strategy"])
    op.create_index("ix_factor_evidence_cards_computed_at", _TABLE, ["computed_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)
