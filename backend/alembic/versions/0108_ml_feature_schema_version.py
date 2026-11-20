"""Add QuantMlModel.feature_schema_version (ADR 0015 Phase 5).

Phase 5 wires WRDS/IBES/insider point-in-time signals into
app.lab.quant_ml.build_features via a new extra_features parameter, which
changes the output column set every already-persisted QuantMlModel artefact
was fitted against. predict_pipeline is strict about matching the fitted
pipeline's feature count/order, so loading an old artefact against the new
feature set would hard-error inside StandardScaler rather than degrade
gracefully. This column lets readers (stage_ml_signal, /ml/models/{id}/predict)
treat a version mismatch exactly like "no model" — the same fail-open path
already used for "ticker never trained yet" — so stale artefacts self-heal
via the weekly discover-ml-training job with zero manual backfill/deletion.

Revision ID: 0108_ml_feature_schema_version
Revises: 0107_trial_ledger
"""

from alembic import op
import sqlalchemy as sa

revision = "0108_ml_feature_schema_version"
down_revision = "0107_trial_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("quant_ml_models"):
        return
    columns = {col["name"] for col in inspector.get_columns("quant_ml_models")}
    if "feature_schema_version" not in columns:
        op.add_column(
            "quant_ml_models",
            sa.Column(
                "feature_schema_version", sa.Integer(), nullable=False, server_default="0"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("quant_ml_models"):
        return
    columns = {col["name"] for col in inspector.get_columns("quant_ml_models")}
    if "feature_schema_version" in columns:
        op.drop_column("quant_ml_models", "feature_schema_version")
