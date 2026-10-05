"""Create fx_rates table for FX-aware valuation (P1).

Daily reference rates keyed (base, quote, date), resolved from the provider
chain and cached so historical snapshots value at their own date's rate.

Revision ID: 0077_fx_rates
Revises: 0076_backfill_jobs
"""
from alembic import op
import sqlalchemy as sa

revision = "0077_fx_rates"
down_revision = "0076_backfill_jobs"
branch_labels = None
depends_on = None


def _index_names(table_name):
    """Return the set of existing index names on ``table_name`` (empty if the table is gone)."""
    inspector = sa.inspect(op.get_bind())
    try:
        return {idx["name"] for idx in inspector.get_indexes(table_name)}
    except sa.exc.NoSuchTableError:
        return set()


def safe_create_index(index_name, table_name, columns, unique=False, **kwargs):
    """Create an index only if it does not already exist (idempotent / duplicate-safe)."""
    resolved = op.f(index_name)
    if resolved not in _index_names(table_name):
        op.create_index(resolved, table_name, columns, unique=unique, **kwargs)


def safe_drop_index(index_name, table_name=None, **kwargs):
    """Drop an index only if it exists (idempotent / safe on re-run)."""
    resolved = op.f(index_name)
    if table_name is not None and resolved in _index_names(table_name):
        op.drop_index(resolved, table_name=table_name, **kwargs)
    elif table_name is None:
        op.drop_index(resolved, **kwargs)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("fx_rates"):
        op.create_table(
            "fx_rates",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("base", sa.String(length=8), nullable=False),
            sa.Column("quote", sa.String(length=8), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("rate", sa.Numeric(20, 10), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("base", "quote", "date", name="uq_fx_rates_base_quote_date"),
        )
        safe_create_index("ix_fx_rates_base", "fx_rates", ["base"])
        safe_create_index("ix_fx_rates_quote", "fx_rates", ["quote"])
        safe_create_index("ix_fx_rates_date", "fx_rates", ["date"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("fx_rates"):
        safe_drop_index("ix_fx_rates_date", table_name="fx_rates")
        safe_drop_index("ix_fx_rates_quote", table_name="fx_rates")
        safe_drop_index("ix_fx_rates_base", table_name="fx_rates")
        op.drop_table("fx_rates")
