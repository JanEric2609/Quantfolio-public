"""Collapse duplicate (account_id, isin) DkbPosition rows.

FinTS adapter can emit duplicate holdings for the same ISIN within a
single sync.  This migration collapses same-(account_id, isin) rows,
retaining the row with the lowest id and summing quantity / value.

Revision ID: 0048_dkb_position_dedupe
Revises: 0047_add_paper_portfolio_tables
Create Date: 2026-06-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0048_dkb_position_dedupe"
down_revision = "0047_add_paper_portfolio_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("dkb_positions"):
        return

    # 1. Find all (account_id, isin) groups that have >1 row
    rows = bind.execute(
        sa.text(
            """
            SELECT account_id, isin, COUNT(*) as cnt
            FROM dkb_positions
            GROUP BY account_id, isin
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()

    for account_id, isin, cnt in rows:
        # Get the row with the lowest id (oldest)
        keep = bind.execute(
            sa.text(
                """
                SELECT id, quantity, avg_buy_price, current_price, current_value, ticker, name
                FROM dkb_positions
                WHERE account_id = :aid AND isin = :isin
                ORDER BY id ASC
                LIMIT 1
                """
            ),
            {"aid": account_id, "isin": isin},
        ).fetchone()

        if not keep:
            continue

        keep_id = keep[0]
        sum_quantity = keep[1]
        sum_avg_buy = keep[2]
        sum_current_price = keep[3]
        sum_current_value = keep[4]
        keep_ticker = keep[5]
        keep_name = keep[6]

        # Sum quantity and value from all duplicates (including the kept row for safety)
        agg = bind.execute(
            sa.text(
                """
                SELECT SUM(quantity), SUM(current_value), SUM(avg_buy_price)
                FROM dkb_positions
                WHERE account_id = :aid AND isin = :isin
                """
            ),
            {"aid": account_id, "isin": isin},
        ).fetchone()

        sum_avg = sum_avg_buy
        sum_value = sum_current_value
        if agg:
            sum_quantity = agg[0] if agg[0] is not None else sum_quantity
            sum_value = agg[1] if agg[1] is not None else sum_current_value
            sum_avg = agg[2] if agg[2] is not None else sum_avg_buy

        # Compute weighted average buy price (keep None when all prices are NULL)
        weighted_avg = None
        if sum_quantity and sum_quantity > 0 and sum_avg is not None:
            weighted_avg = float(sum(  # noqa: This is a migration, float precision acceptable
                c * p for c, p in
                bind.execute(
                    sa.text(
                        """
                        SELECT quantity, COALESCE(avg_buy_price, 0)
                        FROM dkb_positions
                        WHERE account_id = :aid AND isin = :isin
                        """
                    ),
                    {"aid": account_id, "isin": isin},
                ).fetchall()
            )) / float(sum_quantity)

        # Delete all duplicates for this group
        bind.execute(
            sa.text(
                """
                DELETE FROM dkb_positions
                WHERE account_id = :aid AND isin = :isin
                """
            ),
            {"aid": account_id, "isin": isin},
        )

        # Re-insert the merged row
        bind.execute(
            sa.text(
                """
                INSERT INTO dkb_positions (id, account_id, isin, ticker, name, quantity, avg_buy_price, current_price, current_value, last_synced, updated_at)
                VALUES (:id, :aid, :isin, :ticker, :name, :qty, :avg, :price, :val, :last, :upd)
                """
            ),
            {
                "id": keep_id,
                "aid": account_id,
                "isin": isin,
                "ticker": keep_ticker,
                "name": keep_name,
                "qty": float(sum_quantity),
                "avg": float(weighted_avg) if weighted_avg is not None else None,
                "price": float(sum_current_price) if sum_current_price is not None else None,
                "val": float(sum_value) if sum_value is not None else None,
                "last": None,
                "upd": None,
            },
        )


def downgrade() -> None:
    # No reverse; data consolidation is one-way
    pass
