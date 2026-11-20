"""Purge the retired experimental_features_enabled setting row.

The experimental feature gate was removed end-to-end (plan todo 16):
require_experimental deps, inline checks, worker gating, the default key
and catalog entry are gone from app code. This migration deletes any
stored app_settings row so stale prod databases stop carrying the flag.
Guarded + rerun-safe: a no-op when the row is absent.

Revision ID: 0099_purge_experimental_setting
Revises: 0098_drop_review_items
"""

from alembic import op
import sqlalchemy as sa

revision = "0099_purge_experimental_setting"
down_revision = "0098_drop_review_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM app_settings WHERE key = 'experimental_features_enabled'")
    )


def downgrade() -> None:
    # The default row is recreated lazily by settings resolution; nothing to do.
    pass
