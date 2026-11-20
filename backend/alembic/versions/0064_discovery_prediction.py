"""Add discovery_prediction ledger table (Discovery P0 — #111).

Immutable forward-prediction ledger: one row per shortlist candidate per
discovery run. Captures the point-in-time signal snapshot + price so outcomes
can later be scored without lookahead bias. Outcome columns stay NULL until the
P1 resolution job fills them.

Revision ID: 0064_discovery_prediction
Revises: 0063_backfill_baseline_value
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0064_discovery_prediction"
down_revision = "0063_backfill_baseline_value"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("discovery_prediction"):
        features_default = sa.text("'{}'::jsonb") if bind.dialect.name == "postgresql" else "{}"

        op.create_table(
            "discovery_prediction",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("run_id", sa.String(36), nullable=False, index=True),
            sa.Column("asset_id", sa.String(36), nullable=True),
            sa.Column("symbol", sa.String(32), nullable=False, index=True),
            sa.Column("isin", sa.String(16), nullable=True),
            sa.Column("predicted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("horizon_days", sa.Integer, nullable=False),
            sa.Column("resolve_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("direction", sa.String(8), nullable=False, server_default="buy"),
            sa.Column("conviction", sa.Float, nullable=True),
            sa.Column("conviction_calibrated", sa.Float, nullable=True),
            sa.Column("expected_return", sa.Float, nullable=True),
            sa.Column("expected_return_low", sa.Float, nullable=True),
            sa.Column("expected_return_high", sa.Float, nullable=True),
            sa.Column("thesis", sa.Text, nullable=True),
            sa.Column("risks", sa.Text, nullable=True),
            sa.Column("features_json", sa.JSON, nullable=False, server_default=features_default),
            sa.Column("price_at_prediction", sa.Float, nullable=True),
            sa.Column("realised_return", sa.Float, nullable=True),
            sa.Column("outcome_status", sa.String(16), nullable=False, server_default="pending", index=True),
            sa.Column("score_json", sa.JSON, nullable=True),
        )


def downgrade() -> None:
    op.drop_table("discovery_prediction")
