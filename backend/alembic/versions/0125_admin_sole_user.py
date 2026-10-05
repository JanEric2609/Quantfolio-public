"""Give the only account the admin role.

Accounts created before the first-run setup token (the first registration now
gets ``admin``) kept the role ``user``, so on a single-user install nobody could
reach the Discover admin and repair endpoints, or set up Scalable Capital from
the Control Center. When exactly one user exists and nobody is admin yet, that
user becomes admin. Installs with several accounts are left alone; promote one
by hand (docs/deployment.md).

Downgrade does nothing: the role column predates this revision, and taking the
role away again would lock the owner out of the same endpoints.

Revision ID: 0125_admin_sole_user
Revises: 0124_trust_ledgers
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0125_admin_sole_user"
down_revision = "0124_trust_ledgers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return
    if "role" not in {column["name"] for column in inspector.get_columns("users")}:
        return
    users = bind.execute(sa.text("SELECT count(*) FROM users")).scalar() or 0
    admins = bind.execute(sa.text("SELECT count(*) FROM users WHERE role = 'admin'")).scalar() or 0
    if users == 1 and admins == 0:
        bind.execute(sa.text("UPDATE users SET role = 'admin'"))


def downgrade() -> None:
    pass
