"""drop GoCardless and dkb-robo-only tables

Revision ID: 0008_drop_legacy_dkb_tables
Revises: 0007_tax_cockpit
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_drop_legacy_dkb_tables"
down_revision = "0007_tax_cockpit"
branch_labels = None
depends_on = None

_TABLES = [
    "gocardless_requisitions",
    "dkb_standing_orders",
    "dkb_exemption_orders",
    "dkb_postbox_documents",
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in _TABLES:
        if inspector.has_table(table):
            op.drop_table(table)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("gocardless_requisitions"):
        op.create_table(
            "gocardless_requisitions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("requisition_id", sa.String(80), nullable=False, unique=True),
            sa.Column("institution_id", sa.String(80), nullable=False),
            sa.Column("agreement_id", sa.String(80), nullable=True),
            sa.Column("reference", sa.String(120), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="CR"),
            sa.Column("link", sa.Text(), nullable=True),
            sa.Column("redirect_url", sa.Text(), nullable=True),
            sa.Column("accounts_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("consent_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not inspector.has_table("dkb_standing_orders"):
        op.create_table(
            "dkb_standing_orders",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("external_key", sa.String(64), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("amount", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
            sa.Column("next_execution", sa.Date(), nullable=True),
            sa.Column("account_iban", sa.String(34), nullable=True),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "external_key", name="uq_dkb_standing_user_key"),
        )

    if not inspector.has_table("dkb_exemption_orders"):
        op.create_table(
            "dkb_exemption_orders",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("amount", sa.Numeric(20, 6), nullable=False, server_default="0"),
            sa.Column("used_amount", sa.Numeric(20, 6), nullable=True),
            sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", name="uq_dkb_exemption_user"),
        )

    if not inspector.has_table("dkb_postbox_documents"):
        op.create_table(
            "dkb_postbox_documents",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("document_key", sa.String(80), nullable=False),
            sa.Column("title", sa.String(250), nullable=False),
            sa.Column("document_date", sa.Date(), nullable=True),
            sa.Column("unread", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("file_name", sa.String(250), nullable=True),
            sa.Column("encrypted_path", sa.Text(), nullable=True),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "document_key", name="uq_dkb_postbox_user_key"),
        )
