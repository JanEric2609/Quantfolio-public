"""Add user session version for JWT revocation.

Revision ID: 0026_user_session_version
Revises: 0025_verification_loop_state
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa


revision = "0026_user_session_version"
down_revision = "0025_verification_loop_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    if "session_version" not in existing:
        op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    if "session_version" in existing:
        op.drop_column("users", "session_version")
