"""dkb ai budget quant refinements

Revision ID: 0003_dkb_ai_budget_quant
Revises: 0002_reports_context
Create Date: 2026-05-16
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_dkb_ai_budget_quant"
down_revision = "0002_reports_context"
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
    if not inspector.has_table("dkb_sync_logs"):
        op.create_table(
            "dkb_sync_logs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=24), nullable=False, server_default="fints"),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_dkb_sync_logs_user_id", "dkb_sync_logs", ["user_id"])
        safe_create_index("ix_dkb_sync_logs_session_id", "dkb_sync_logs", ["session_id"])
    if not inspector.has_table("dkb_standing_orders"):
        op.create_table(
            "dkb_standing_orders",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("external_key", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("amount", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
            sa.Column("next_execution", sa.Date(), nullable=True),
            sa.Column("account_iban", sa.String(length=34), nullable=True),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "external_key", name="uq_dkb_standing_user_key"),
        )
        safe_create_index("ix_dkb_standing_orders_user_id", "dkb_standing_orders", ["user_id"])
        safe_create_index("ix_dkb_standing_orders_external_key", "dkb_standing_orders", ["external_key"])
    if not inspector.has_table("dkb_exemption_orders"):
        op.create_table(
            "dkb_exemption_orders",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("amount", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("used_amount", sa.Numeric(20, 6), nullable=True),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", name="uq_dkb_exemption_user"),
        )
        safe_create_index("ix_dkb_exemption_orders_user_id", "dkb_exemption_orders", ["user_id"])
    if not inspector.has_table("dkb_postbox_documents"):
        op.create_table(
            "dkb_postbox_documents",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("document_key", sa.String(length=80), nullable=False),
            sa.Column("title", sa.String(length=250), nullable=False),
            sa.Column("document_date", sa.Date(), nullable=True),
            sa.Column("unread", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("file_name", sa.String(length=250), nullable=True),
            sa.Column("encrypted_path", sa.Text(), nullable=True),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "document_key", name="uq_dkb_postbox_user_key"),
        )
        safe_create_index("ix_dkb_postbox_documents_user_id", "dkb_postbox_documents", ["user_id"])
        safe_create_index("ix_dkb_postbox_documents_document_key", "dkb_postbox_documents", ["document_key"])
    if not inspector.has_table("recommendation_attempts"):
        op.create_table(
            "recommendation_attempts",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("ticker", sa.String(length=32), nullable=True),
            sa.Column("horizon", sa.String(length=24), nullable=False, server_default="mid"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="failed"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("debug_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        safe_create_index("ix_recommendation_attempts_user_id", "recommendation_attempts", ["user_id"])
        safe_create_index("ix_recommendation_attempts_ticker", "recommendation_attempts", ["ticker"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (
        "recommendation_attempts",
        "dkb_postbox_documents",
        "dkb_exemption_orders",
        "dkb_standing_orders",
        "dkb_sync_logs",
    ):
        if inspector.has_table(table):
            op.drop_table(table)
