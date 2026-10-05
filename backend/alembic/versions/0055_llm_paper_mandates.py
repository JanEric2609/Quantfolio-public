"""Add LLM paper portfolio mandates, decisions, and advice cards.

Revision ID: 0055_llm_paper_mandates
Revises: 0054_holdings_unique
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0055_llm_paper_mandates"
down_revision = "0054_holdings_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # --- Modify paper_portfolios table (SQLite-compatible batch alter) ---
    if inspector.has_table("paper_portfolios"):
        with op.batch_alter_table("paper_portfolios") as batch_op:
            # Drop old unique constraint on user_id if it exists
            existing_constraints = {c["name"] for c in inspector.get_unique_constraints("paper_portfolios")}
            if "uq_paper_portfolio_user" in existing_constraints:
                batch_op.drop_constraint("uq_paper_portfolio_user", type_="unique")

            # Add mandate column (nullable, NULL = legacy)
            if not any(c["name"] == "mandate" for c in inspector.get_columns("paper_portfolios")):
                batch_op.add_column(sa.Column("mandate", sa.String(24), nullable=True))

            # Add managed_by column (non-null, default "manual")
            if not any(c["name"] == "managed_by" for c in inspector.get_columns("paper_portfolios")):
                batch_op.add_column(sa.Column("managed_by", sa.String(24), nullable=False, server_default="manual"))

            # Add mandate_config_json column (non-null, default "{}")
            if not any(c["name"] == "mandate_config_json" for c in inspector.get_columns("paper_portfolios")):
                batch_op.add_column(sa.Column("mandate_config_json", sa.Text, nullable=False, server_default="{}"))

            # Add new unique constraint on (user_id, mandate)
            if "uq_paper_portfolio_user_mandate" not in existing_constraints:
                batch_op.create_unique_constraint("uq_paper_portfolio_user_mandate", ["user_id", "mandate"])

    # --- Create llm_portfolio_decisions table ---
    if not inspector.has_table("llm_portfolio_decisions"):
        op.create_table(
            "llm_portfolio_decisions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("portfolio_id", sa.String(36), sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False),
            sa.Column("review_date", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("mandate", sa.String(24), nullable=True),
            sa.Column("decision_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("reflection_json", sa.Text, nullable=True),
            sa.Column("context_token_estimate", sa.Integer, nullable=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
        )

    # --- Create llm_advice_cards table ---
    if not inspector.has_table("llm_advice_cards"):
        op.create_table(
            "llm_advice_cards",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("portfolio_id", sa.String(36), sa.ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False),
            sa.Column("divergence_json", sa.Text, nullable=False, server_default="{}"),
            sa.Column("advice_text", sa.Text, nullable=False, server_default=""),
            sa.Column("performance_delta", sa.Numeric(8, 4), nullable=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="new"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop llm_advice_cards
    if inspector.has_table("llm_advice_cards"):
        op.drop_table("llm_advice_cards")

    # Drop llm_portfolio_decisions
    if inspector.has_table("llm_portfolio_decisions"):
        op.drop_table("llm_portfolio_decisions")

    # Revert paper_portfolios changes
    if inspector.has_table("paper_portfolios"):
        with op.batch_alter_table("paper_portfolios") as batch_op:
            existing_constraints = {c["name"] for c in inspector.get_unique_constraints("paper_portfolios")}
            existing_columns = {c["name"] for c in inspector.get_columns("paper_portfolios")}

            # Drop new unique constraint
            if "uq_paper_portfolio_user_mandate" in existing_constraints:
                batch_op.drop_constraint("uq_paper_portfolio_user_mandate", type_="unique")

            # Drop added columns
            for col in ("mandate_config_json", "managed_by", "mandate"):
                if col in existing_columns:
                    batch_op.drop_column(col)

            # Re-add old unique constraint
            if "uq_paper_portfolio_user" not in existing_constraints:
                dupes = bind.execute(sa.text("""
                    SELECT user_id, COUNT(*) as cnt
                    FROM paper_portfolios
                    GROUP BY user_id
                    HAVING COUNT(*) > 1
                """)).fetchall()
                if dupes:
                    raise RuntimeError(
                        "Cannot downgrade: duplicate user_id values exist in paper_portfolios. "
                        "Resolve duplicates before downgrading."
                    )
                batch_op.create_unique_constraint("uq_paper_portfolio_user", ["user_id"])
