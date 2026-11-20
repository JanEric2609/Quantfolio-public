"""Add role column to users table for admin/user differentiation.

Revision ID: 0046_add_user_role
Revises: 0045_snapshot_portfolio_id
Create Date: 2026-06-10
"""

from alembic import op
import sqlalchemy as sa


revision = "0046_add_user_role"
down_revision = "0045_snapshot_portfolio_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    if "role" not in existing:
        op.add_column("users", sa.Column("role", sa.String(16), nullable=False, server_default="user"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    if "role" in existing:
        op.drop_column("users", "role")
