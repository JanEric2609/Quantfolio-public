"""derivative_terms + derivative_quotes: warrants held at a broker (ADR 0026).

Additive only.

Revision ID: 0156_derivatives
Revises: 0155_learning_purchases
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0156_derivatives"
down_revision = "0155_learning_purchases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("derivative_terms"):
        op.create_table(
            "derivative_terms",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("isin", sa.String(12), nullable=False),
            sa.Column("kind", sa.String(8), nullable=False),
            sa.Column("style", sa.String(12), nullable=False, server_default="american"),
            sa.Column("strike", sa.Numeric(20, 6), nullable=False),
            sa.Column("strike_currency", sa.String(3), nullable=False, server_default="USD"),
            sa.Column("ratio", sa.Numeric(20, 8), nullable=False),
            sa.Column("expiry", sa.Date(), nullable=False),
            sa.Column("underlying_symbol", sa.String(32), nullable=False),
            sa.Column("issuer", sa.String(64), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "isin", name="uq_derivative_terms_user_isin"),
        )
        op.create_index("ix_derivative_terms_user_id", "derivative_terms", ["user_id"])
        op.create_index("ix_derivative_terms_isin", "derivative_terms", ["isin"])
    if not inspector.has_table("derivative_quotes"):
        op.create_table(
            "derivative_quotes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("isin", sa.String(12), nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("underlying_symbol", sa.String(32), nullable=False),
            sa.Column("warrant_price_eur", sa.Float(), nullable=False),
            sa.Column("underlying_close", sa.Float(), nullable=True),
            sa.Column("eur_per_unit", sa.Float(), nullable=True),
            sa.Column("days_to_expiry", sa.Integer(), nullable=True),
            sa.Column("warrant_iv", sa.Float(), nullable=True),
            sa.Column("listed_iv", sa.Float(), nullable=True),
            sa.Column("listed_strike", sa.Float(), nullable=True),
            sa.Column("listed_expiry", sa.Date(), nullable=True),
            sa.Column("source", sa.String(32), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("isin", "day", name="uq_derivative_quotes_isin_day"),
        )
        op.create_index("ix_derivative_quotes_isin", "derivative_quotes", ["isin"])
        op.create_index("ix_derivative_quotes_day", "derivative_quotes", ["day"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("derivative_quotes"):
        op.drop_index("ix_derivative_quotes_day", table_name="derivative_quotes")
        op.drop_index("ix_derivative_quotes_isin", table_name="derivative_quotes")
        op.drop_table("derivative_quotes")
    if inspector.has_table("derivative_terms"):
        op.drop_index("ix_derivative_terms_isin", table_name="derivative_terms")
        op.drop_index("ix_derivative_terms_user_id", table_name="derivative_terms")
        op.drop_table("derivative_terms")
