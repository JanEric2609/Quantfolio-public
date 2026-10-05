"""dkb gocardless requisitions

Revision ID: 0006_dkb_gocardless
Revises: 0005_wealth_cockpit
Create Date: 2026-05-18
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_dkb_gocardless"
down_revision = "0005_wealth_cockpit"
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

    if not inspector.has_table("gocardless_requisitions"):
        op.create_table(
            "gocardless_requisitions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("requisition_id", sa.String(length=80), nullable=False),
            sa.Column("institution_id", sa.String(length=80), nullable=False),
            sa.Column("agreement_id", sa.String(length=80), nullable=True),
            sa.Column("reference", sa.String(length=120), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="CR"),
            sa.Column("link", sa.Text(), nullable=True),
            sa.Column("redirect_url", sa.Text(), nullable=True),
            sa.Column("accounts_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("consent_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("requisition_id", name="uq_gocardless_requisitions_requisition_id"),
        )
        safe_create_index("ix_gocardless_requisitions_user_id", "gocardless_requisitions", ["user_id"])
        safe_create_index("ix_gocardless_requisitions_status", "gocardless_requisitions", ["status"])
        safe_create_index("ix_gocardless_requisitions_institution_id", "gocardless_requisitions", ["institution_id"])
        safe_create_index("ix_gocardless_requisitions_user_status", "gocardless_requisitions", ["user_id", "status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("gocardless_requisitions"):
        op.drop_table("gocardless_requisitions")
