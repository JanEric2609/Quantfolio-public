"""add quant_ml_models table

Revision ID: 0009_quant_ml
Revises: 0008_drop_legacy_dkb_tables
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_quant_ml"
down_revision = "0008_drop_legacy_dkb_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("quant_ml_models"):
        op.create_table(
            "quant_ml_models",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False, server_default="lgbm"),
            sa.Column("params_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("artefact_path", sa.Text, nullable=True),
            sa.Column("metrics_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("status", sa.String(24), nullable=False, server_default="created", index=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("quant_ml_models"):
        op.drop_table("quant_ml_models")
