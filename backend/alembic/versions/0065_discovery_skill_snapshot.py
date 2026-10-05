"""Add discovery_skill_snapshot table (Discovery P1 — #112).

Periodic aggregate skill metrics computed from resolved DiscoveryPrediction
rows — Rank IC, Brier score, hit-rate, ECE, Mincer parameters — so the UI
can surface a "is the system improving?" trend chart.

Revision ID: 0065_discovery_skill_snapshot
Revises: 0064_discovery_prediction
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0065_discovery_skill_snapshot"
down_revision = "0064_discovery_prediction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("discovery_skill_snapshot"):
        details_default = sa.text("'{}'::jsonb") if bind.dialect.name == "postgresql" else "{}"

        op.create_table(
            "discovery_skill_snapshot",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), index=True),
            sa.Column("metric_type", sa.String(32), nullable=False, server_default="rolling_12m"),
            sa.Column("total_predictions", sa.Integer, nullable=False, server_default="0"),
            sa.Column("total_resolved", sa.Integer, nullable=False, server_default="0"),
            sa.Column("hit_rate", sa.Float, nullable=True),
            sa.Column("brier_score_avg", sa.Float, nullable=True),
            sa.Column("rank_ic", sa.Float, nullable=True),
            sa.Column("icir", sa.Float, nullable=True),
            sa.Column("ece", sa.Float, nullable=True),
            sa.Column("mincer_a0", sa.Float, nullable=True),
            sa.Column("mincer_a1", sa.Float, nullable=True),
            sa.Column("details_json", sa.JSON, nullable=False, server_default=details_default),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )


def downgrade() -> None:
    op.drop_table("discovery_skill_snapshot")
