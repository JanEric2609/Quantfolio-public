"""Purge the legacy ``reddit`` ApiKey row.

The Reddit integration (market-sentiment via the Reddit script-app API) has been
removed from the product. Any stored ``reddit`` credential is now dead data — the
service is no longer a valid ``IntegrationTestRequest`` target, has no catalog
entry, and is excluded from ``SENSITIVE_INTEGRATIONS``. Data-only, idempotent.

Revision ID: 0058_purge_reddit_apikey
Revises: 0057_target_allocations
Create Date: 2026-06-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0058_purge_reddit_apikey"
down_revision = "0057_target_allocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("api_keys"):
        op.execute("DELETE FROM api_keys WHERE service = 'reddit'")


def downgrade() -> None:
    pass  # data-only purge of a removed integration; nothing to restore
