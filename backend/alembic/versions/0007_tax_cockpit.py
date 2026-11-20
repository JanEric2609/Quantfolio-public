"""tax cockpit tables

Revision ID: 0007_tax_cockpit
Revises: 0006_dkb_gocardless
Create Date: 2026-05-18
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_tax_cockpit"
down_revision = "0006_dkb_gocardless"
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

    if not inspector.has_table("tax_lots"):
        op.create_table(
            "tax_lots",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("isin", sa.String(length=12), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=True),
            sa.Column("name", sa.String(length=200), nullable=True),
            sa.Column("account_ref", sa.String(length=120), nullable=True),
            sa.Column("fund_class", sa.String(length=24), nullable=False, server_default="other"),
            sa.Column("teilfreistellung_pct", sa.Numeric(6, 4), nullable=False, server_default="0"),
            sa.Column("acquired_at", sa.Date(), nullable=False),
            sa.Column("quantity_initial", sa.Numeric(20, 8), nullable=False),
            sa.Column("quantity_remaining", sa.Numeric(20, 8), nullable=False),
            sa.Column("cost_basis_eur", sa.Numeric(20, 6), nullable=False),
            sa.Column("fees_eur", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("fx_rate", sa.Numeric(20, 8), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
            sa.Column("source_ref", sa.String(length=120), nullable=True),
            sa.Column("closed_at", sa.Date(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_tax_lots_user_id", "tax_lots", ["user_id"])
        safe_create_index("ix_tax_lots_isin", "tax_lots", ["isin"])
        safe_create_index("ix_tax_lots_acquired_at", "tax_lots", ["acquired_at"])
        safe_create_index("ix_tax_lots_user_isin", "tax_lots", ["user_id", "isin"])
        safe_create_index("ix_tax_lots_user_open", "tax_lots", ["user_id", "closed_at"])

    if not inspector.has_table("tax_ledger_events"):
        op.create_table(
            "tax_ledger_events",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tax_year", sa.Integer(), nullable=False),
            sa.Column("event_date", sa.Date(), nullable=False),
            sa.Column("event_type", sa.String(length=32), nullable=False),
            sa.Column("isin", sa.String(length=12), nullable=True),
            sa.Column("symbol", sa.String(length=32), nullable=True),
            sa.Column("name", sa.String(length=200), nullable=True),
            sa.Column("fund_class", sa.String(length=24), nullable=True),
            sa.Column("teilfreistellung_pct", sa.Numeric(6, 4), nullable=False, server_default="0"),
            sa.Column("gross_eur", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("withheld_eur", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("foreign_wht_eur", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("foreign_country", sa.String(length=2), nullable=True),
            sa.Column("realised_gain_eur", sa.Numeric(20, 6), nullable=True),
            sa.Column("bucket", sa.String(length=24), nullable=True),
            sa.Column("confidence", sa.String(length=16), nullable=False, server_default="estimate"),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
            sa.Column("source_ref", sa.String(length=120), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_tax_ledger_events_user_id", "tax_ledger_events", ["user_id"])
        safe_create_index("ix_tax_ledger_events_tax_year", "tax_ledger_events", ["tax_year"])
        safe_create_index("ix_tax_ledger_events_event_date", "tax_ledger_events", ["event_date"])
        safe_create_index("ix_tax_ledger_events_event_type", "tax_ledger_events", ["event_type"])
        safe_create_index("ix_tax_ledger_events_isin", "tax_ledger_events", ["isin"])
        safe_create_index("ix_tax_ledger_user_year", "tax_ledger_events", ["user_id", "tax_year"])
        safe_create_index("ix_tax_ledger_user_type", "tax_ledger_events", ["user_id", "event_type"])

    if not inspector.has_table("tax_year_summaries"):
        op.create_table(
            "tax_year_summaries",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tax_year", sa.Integer(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "tax_year", name="uq_tax_year_summaries_user_year"),
        )
        safe_create_index("ix_tax_year_summaries_user_id", "tax_year_summaries", ["user_id"])
        safe_create_index("ix_tax_year_summaries_tax_year", "tax_year_summaries", ["tax_year"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("tax_year_summaries", "tax_ledger_events", "tax_lots"):
        if inspector.has_table(table):
            op.drop_table(table)
