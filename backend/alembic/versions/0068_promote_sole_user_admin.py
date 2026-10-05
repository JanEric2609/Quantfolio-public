"""Promote sole user to admin role for discovery config endpoints

Revision ID: 0068_promote_sole_user_admin
Revises: 0067_discovery_config_registry
Create Date: 2026-06-17

Rationale: Discovery config endpoints (perturb, review) require admin role.
In single-user self-hosted setups, this migration promotes the sole user
to admin so they can use these endpoints. No email/PII hardcoded.
"""
from alembic import op
import sqlalchemy as sa


revision = "0068_promote_sole_user_admin"
down_revision = "0067_discovery_config_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Only proceed if users table exists and role column exists
    if not inspector.has_table("users"):
        return

    columns = [c["name"] for c in inspector.get_columns("users")]
    if "role" not in columns:
        return

    # Count users in the database
    result = bind.execute(sa.text("SELECT COUNT(id) as cnt FROM users"))
    count_row = result.fetchone()
    user_count = count_row[0] if count_row else 0

    # If exactly one user exists and not already admin, promote them
    if user_count == 1:
        result = bind.execute(sa.text("SELECT id, role FROM users LIMIT 1"))
        user_row = result.fetchone()
        if user_row:
            user_id, current_role = user_row
            if current_role != "admin":
                bind.execute(
                    sa.text("UPDATE users SET role = :role WHERE id = :user_id"),
                    {"role": "admin", "user_id": user_id}
                )
    elif user_count == 0:
        # No users to promote
        pass
    else:
        # Multiple users exist; do nothing (not safe to promote without knowing which user)
        pass


def downgrade() -> None:
    # Safe no-op: cannot reliably reverse a role promotion.
    # If this migration is rolled back, the admin role will remain.
    pass
