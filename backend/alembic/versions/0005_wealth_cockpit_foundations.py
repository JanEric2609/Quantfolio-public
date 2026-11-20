"""wealth cockpit foundations

Revision ID: 0005_wealth_cockpit
Revises: 0004_research_system
Create Date: 2026-05-18
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_wealth_cockpit"
down_revision = "0004_research_system"
branch_labels = None
depends_on = None


def _index_names(inspector: sa.Inspector, table: str) -> set[str]:
    if not inspector.has_table(table):
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


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

    if not inspector.has_table("dkb_diagnostic_runs"):
        op.create_table(
            "dkb_diagnostic_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("provider", sa.String(length=24), nullable=False, server_default="fints"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="warning"),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("steps_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_dkb_diagnostic_runs_user_id", "dkb_diagnostic_runs", ["user_id"])
        safe_create_index("ix_dkb_diagnostic_runs_status", "dkb_diagnostic_runs", ["status"])

    if not inspector.has_table("connected_accounts"):
        op.create_table(
            "connected_accounts",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
            sa.Column("external_id", sa.String(length=120), nullable=True),
            sa.Column("name", sa.String(length=180), nullable=False),
            sa.Column("institution", sa.String(length=120), nullable=True),
            sa.Column("account_type", sa.String(length=32), nullable=False, server_default="cash"),
            sa.Column("iban", sa.String(length=34), nullable=True),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
            sa.Column("balance", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("last_synced", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "source", "external_id", name="uq_connected_accounts_user_source_external"),
        )
        safe_create_index("ix_connected_accounts_user_id", "connected_accounts", ["user_id"])
        safe_create_index("ix_connected_accounts_source", "connected_accounts", ["source"])
        safe_create_index("ix_connected_accounts_iban", "connected_accounts", ["iban"])
        safe_create_index("ix_connected_accounts_user_source", "connected_accounts", ["user_id", "source"])

    if not inspector.has_table("activity_ledger_entries"):
        op.create_table(
            "activity_ledger_entries",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("connected_account_id", sa.String(length=36), sa.ForeignKey("connected_accounts.id", ondelete="SET NULL"), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="manual"),
            sa.Column("external_id", sa.String(length=120), nullable=True),
            sa.Column("dedupe_hash", sa.String(length=64), nullable=False),
            sa.Column("activity_type", sa.String(length=32), nullable=False, server_default="cashflow"),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("amount", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
            sa.Column("description", sa.String(length=300), nullable=False),
            sa.Column("isin", sa.String(length=12), nullable=True),
            sa.Column("symbol", sa.String(length=32), nullable=True),
            sa.Column("quantity", sa.Numeric(20, 8), nullable=True),
            sa.Column("price", sa.Numeric(20, 6), nullable=True),
            sa.Column("fees", sa.Numeric(20, 6), nullable=True),
            sa.Column("review_state", sa.String(length=24), nullable=False, server_default="trusted"),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "source", "dedupe_hash", name="uq_activity_ledger_user_source_hash"),
        )
        safe_create_index("ix_activity_ledger_entries_user_id", "activity_ledger_entries", ["user_id"])
        safe_create_index("ix_activity_ledger_entries_source", "activity_ledger_entries", ["source"])
        safe_create_index("ix_activity_ledger_entries_dedupe_hash", "activity_ledger_entries", ["dedupe_hash"])
        safe_create_index("ix_activity_ledger_entries_date", "activity_ledger_entries", ["date"])
        safe_create_index("ix_activity_ledger_entries_review_state", "activity_ledger_entries", ["review_state"])
        safe_create_index("ix_activity_ledger_entries_isin", "activity_ledger_entries", ["isin"])
        safe_create_index("ix_activity_ledger_entries_symbol", "activity_ledger_entries", ["symbol"])
        safe_create_index("ix_activity_ledger_user_date", "activity_ledger_entries", ["user_id", "date"])
        safe_create_index("ix_activity_ledger_user_review", "activity_ledger_entries", ["user_id", "review_state"])

    if not inspector.has_table("portfolio_snapshots"):
        op.create_table(
            "portfolio_snapshots",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("total_value", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("cash_value", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("security_value", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
            sa.Column("source", sa.String(length=32), nullable=False, server_default="computed"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "date", "source", name="uq_portfolio_snapshots_user_date_source"),
        )
        safe_create_index("ix_portfolio_snapshots_user_id", "portfolio_snapshots", ["user_id"])
        safe_create_index("ix_portfolio_snapshots_date", "portfolio_snapshots", ["date"])
        safe_create_index("ix_portfolio_snapshots_user_date", "portfolio_snapshots", ["user_id", "date"])

    if not inspector.has_table("provider_health"):
        op.create_table(
            "provider_health",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("capability", sa.String(length=40), nullable=False, server_default="status"),
            sa.Column("configured", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="unknown"),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("provider", "capability", name="uq_provider_health_provider_capability"),
        )
        safe_create_index("ix_provider_health_provider", "provider_health", ["provider"])
        safe_create_index("ix_provider_health_capability", "provider_health", ["capability"])
        safe_create_index("ix_provider_health_status", "provider_health", ["status"])

    if not inspector.has_table("review_items"):
        op.create_table(
            "review_items",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column("item_type", sa.String(length=40), nullable=False),
            sa.Column("title", sa.String(length=220), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
            sa.Column("severity", sa.String(length=24), nullable=False, server_default="info"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_review_items_user_id", "review_items", ["user_id"])
        safe_create_index("ix_review_items_source", "review_items", ["source"])
        safe_create_index("ix_review_items_item_type", "review_items", ["item_type"])
        safe_create_index("ix_review_items_status", "review_items", ["status"])
        safe_create_index("ix_review_items_user_status", "review_items", ["user_id", "status"])
        safe_create_index("ix_review_items_user_source", "review_items", ["user_id", "source"])

    # Ensure re-running guarded migrations remains harmless if an earlier manual schema exists.
    for table, index, columns in (
        ("provider_health", "ix_provider_health_status", ["status"]),
        ("review_items", "ix_review_items_user_status", ["user_id", "status"]),
    ):
        if inspector.has_table(table) and index not in _index_names(inspector, table):
            safe_create_index(index, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (
        "review_items",
        "provider_health",
        "portfolio_snapshots",
        "activity_ledger_entries",
        "connected_accounts",
        "dkb_diagnostic_runs",
    ):
        if inspector.has_table(table):
            op.drop_table(table)
