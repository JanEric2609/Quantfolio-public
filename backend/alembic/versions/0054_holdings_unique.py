"""Add unique constraints on holdings and dkb_positions, deduplicate existing data.

Revision ID: 0054_holdings_unique
Revises: 0053_news_relevance
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0054_holdings_unique"
down_revision = "0053_news_relevance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Step 1: Detect duplicate portfolio rows per user (manual resolution required)
    if inspector.has_table("portfolios"):
        dupes = bind.execute(sa.text("""
            SELECT user_id, COUNT(*) as cnt
            FROM portfolios
            GROUP BY user_id
            HAVING COUNT(*) > 1
        """)).fetchall()
        if dupes:
            examples = []
            for row in dupes[:5]:
                pids = bind.execute(
                    sa.text("SELECT id FROM portfolios WHERE user_id = :uid"),
                    {"uid": row.user_id}
                ).scalars().all()
                examples.append(f"user_id={row.user_id} (portfolios: {', '.join(pids)})")
            raise RuntimeError(
                f"Migration 0054 blocked: {len(dupes)} user(s) have multiple portfolios. "
                f"Manually resolve duplicates before re-running. Examples: {'; '.join(examples)}"
            )

    # Step 2: For remaining duplicate holdings within same portfolio+isin, keep the one with highest quantity
    if inspector.has_table("holdings"):
        # Re-home transactions to the survivor before deleting duplicates
        if inspector.has_table("transactions_log"):
            op.execute("""
                UPDATE transactions_log
                SET holding_id = (
                    SELECT h1.id
                    FROM holdings h1
                    WHERE h1.portfolio_id = (
                        SELECT portfolio_id FROM holdings h3 WHERE h3.id = transactions_log.holding_id
                    )
                    AND h1.isin = (
                        SELECT isin FROM holdings h3 WHERE h3.id = transactions_log.holding_id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM holdings h2
                        WHERE h2.portfolio_id = h1.portfolio_id AND h2.isin = h1.isin
                        AND (h2.quantity > h1.quantity OR (h2.quantity = h1.quantity AND h2.id < h1.id))
                    )
                    LIMIT 1
                )
                WHERE holding_id IN (
                    SELECT h2.id
                    FROM holdings h1
                    JOIN holdings h2 ON h2.portfolio_id = h1.portfolio_id AND h2.isin = h1.isin
                    WHERE h2.quantity < h1.quantity OR (h2.quantity = h1.quantity AND h2.id > h1.id)
                )
            """)
        op.execute("""
            DELETE FROM holdings WHERE id IN (
                SELECT h2.id FROM holdings h1
                JOIN holdings h2 ON h2.portfolio_id = h1.portfolio_id AND h2.isin = h1.isin
                WHERE h2.quantity < h1.quantity OR (h2.quantity = h1.quantity AND h2.id > h1.id)
            )
        """)

    # Step 3: Add unique constraint on holdings (portfolio_id, isin)
    if inspector.has_table("holdings"):
        existing = {c["name"] for c in inspector.get_unique_constraints("holdings")}
        if "uq_holdings_portfolio_isin" not in existing:
            op.create_unique_constraint("uq_holdings_portfolio_isin", "holdings", ["portfolio_id", "isin"])

    # Step 4: Add unique constraint on dkb_positions (account_id, isin)
    if inspector.has_table("dkb_positions"):
        existing = {c["name"] for c in inspector.get_unique_constraints("dkb_positions")}
        if "uq_dkb_positions_account_isin" not in existing:
            op.execute("""
                DELETE FROM dkb_positions WHERE id IN (
                    SELECT d2.id FROM dkb_positions d1
                    JOIN dkb_positions d2 ON d2.account_id = d1.account_id AND d2.isin = d1.isin
                    WHERE d2.quantity < d1.quantity OR (d2.quantity = d1.quantity AND d2.id > d1.id)
                )
            """)
            op.create_unique_constraint("uq_dkb_positions_account_isin", "dkb_positions", ["account_id", "isin"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("holdings"):
        existing = {c["name"] for c in inspector.get_unique_constraints("holdings")}
        if "uq_holdings_portfolio_isin" in existing:
            op.drop_constraint("uq_holdings_portfolio_isin", "holdings", type_="unique")
    if inspector.has_table("dkb_positions"):
        existing = {c["name"] for c in inspector.get_unique_constraints("dkb_positions")}
        if "uq_dkb_positions_account_isin" in existing:
            op.drop_constraint("uq_dkb_positions_account_isin", "dkb_positions", type_="unique")
