"""Add unique constraints to dkb_accounts and portfolio_snapshots to prevent MultipleResultsFound during DKB sync.

Revision ID: 0051_dkb_constraints
Revises: 0050_discover_runs
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0051_dkb_constraints"
down_revision = "0050_discover_runs"
branch_labels = None
depends_on = None


def _dedup_table(table: str, group_cols: list[str], keep_order: str) -> None:
    """Delete duplicate rows, keeping the one with the highest *keep_order* value."""
    cols = ", ".join(group_cols)
    op.execute(f"""
        DELETE FROM {table}
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY {cols}
                           ORDER BY {keep_order} DESC
                       ) AS rn
                FROM {table}
            ) AS ranked
            WHERE rn = 1
        )
    """)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("dkb_accounts"):
        existing = {c["name"] for c in inspector.get_unique_constraints("dkb_accounts")}
        if "uq_dkb_account_user_iban" not in existing:
            _dedup_table("dkb_accounts", ["user_id", "iban"], "COALESCE(updated_at, last_synced)")
            op.create_unique_constraint(
                "uq_dkb_account_user_iban", "dkb_accounts", ["user_id", "iban"]
            )

    if inspector.has_table("portfolio_snapshots"):
        existing = {c["name"] for c in inspector.get_unique_constraints("portfolio_snapshots")}
        if "uq_portfolio_snapshots_user_date_source" not in existing:
            _dedup_table("portfolio_snapshots", ["user_id", "date", "source"], "created_at")
            op.create_unique_constraint(
                "uq_portfolio_snapshots_user_date_source",
                "portfolio_snapshots",
                ["user_id", "date", "source"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("dkb_accounts"):
        existing = {c["name"] for c in inspector.get_unique_constraints("dkb_accounts")}
        if "uq_dkb_account_user_iban" in existing:
            op.drop_constraint("uq_dkb_account_user_iban", "dkb_accounts", type_="unique")

    if inspector.has_table("portfolio_snapshots"):
        existing = {c["name"] for c in inspector.get_unique_constraints("portfolio_snapshots")}
        if "uq_portfolio_snapshots_user_date_source" in existing:
            op.drop_constraint(
                "uq_portfolio_snapshots_user_date_source",
                "portfolio_snapshots",
                type_="unique",
            )
