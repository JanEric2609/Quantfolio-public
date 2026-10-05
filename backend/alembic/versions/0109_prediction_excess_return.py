"""Add DiscoveryPrediction.excess_return / benchmark_return (Phase 2).

Predictions were scored on the raw price move, so in a rising market every
long pick "hit". Resolution now measures each prediction against the passive
core (EUNL.DE by default) over the same entry and exit closes, in EUR. The
benchmark move and the direction-signed excess return get their own columns
so skill queries do not have to dig through score_json.

Revision ID: 0109_prediction_excess_return
Revises: 0108_ml_feature_schema_version
"""

from alembic import op
import sqlalchemy as sa

revision = "0109_prediction_excess_return"
down_revision = "0108_ml_feature_schema_version"
branch_labels = None
depends_on = None

_TABLE = "discovery_prediction"
_COLUMNS = ("excess_return", "benchmark_return")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    for name in _COLUMNS:
        if name not in existing:
            op.add_column(_TABLE, sa.Column(name, sa.Float(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    existing = {col["name"] for col in inspector.get_columns(_TABLE)}
    for name in _COLUMNS:
        if name in existing:
            op.drop_column(_TABLE, name)
