"""discovery_prediction.superseded_by_run_id: a same-day rerun supersedes earlier picks.

Plan 2026-11-10, WP2d. Two Discover runs on one day stored two sets of picks, so
583 rows came from 24 issue dates. Additive only; the hit rate, the calibrator
and the skill snapshot skip rows where the column is set.

Revision ID: 0149_prediction_superseded
Revises: 0148_ladder_sleeves_cycle_claim
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0149_prediction_superseded"
down_revision = "0148_ladder_sleeves_cycle_claim"
branch_labels = None
depends_on = None

_TABLE = "discovery_prediction"
_COLUMN = "superseded_by_run_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    if _COLUMN not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(36), nullable=True))
    if "ix_discovery_prediction_superseded" not in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.create_index("ix_discovery_prediction_superseded", _TABLE, [_COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    if "ix_discovery_prediction_superseded" in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.drop_index("ix_discovery_prediction_superseded", table_name=_TABLE)
    if _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}:
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
