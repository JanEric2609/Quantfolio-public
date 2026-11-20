"""add quant_rl_policies table

Revision ID: 0010_quant_rl_policies
Revises: 0009_quant_ml
Create Date: 2026-05-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_quant_rl_policies"
down_revision = "0009_quant_ml"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("quant_rl_policies"):
        op.create_table(
            "quant_rl_policies",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("env_id", sa.String(80), nullable=False, index=True),
            sa.Column("algo", sa.String(24), nullable=False, server_default="ppo"),
            sa.Column("params_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("artefact_path", sa.Text, nullable=True),
            sa.Column("training_metrics_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("status", sa.String(24), nullable=False, server_default="created", index=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("quant_rl_policies"):
        op.drop_table("quant_rl_policies")
