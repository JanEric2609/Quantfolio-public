"""Backfill portfolio_snapshots.portfolio_id written without one.

0045 added the column and backfilled it once, but both DKB snapshot writers
(``portfolio_service.snapshot_dkb_positions`` and
``sync_dkb_to_wealth_ledger``)
kept creating rows with only ``user_id``. Verification's risk, stress and
alert modules filter snapshots by ``portfolio_id``, so every snapshot since
2026-06 was invisible to them. The writers now set it; this fills the gap.

Downgrade leaves the ids in place: they are correct data, not schema.

Revision ID: 0122_snapshot_portfolio_backfill
Revises: 0121_control_center_cleanup
"""
from alembic import op
import sqlalchemy as sa

revision = "0122_snapshot_portfolio_backfill"
down_revision = "0121_control_center_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not (inspector.has_table("portfolio_snapshots") and inspector.has_table("portfolios")):
        return
    columns = {c["name"] for c in inspector.get_columns("portfolio_snapshots")}
    if "portfolio_id" not in columns:
        return
    bind.execute(
        sa.text(
            """
            UPDATE portfolio_snapshots
            SET portfolio_id = (
                SELECT id FROM portfolios
                WHERE portfolios.user_id = portfolio_snapshots.user_id
                LIMIT 1
            )
            WHERE portfolio_id IS NULL
            """
        )
    )


def downgrade() -> None:
    pass
