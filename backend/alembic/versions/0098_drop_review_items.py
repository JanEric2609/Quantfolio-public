"""Drop review_items table — full review-inbox removal (plan todo 8).

All writers/readers were removed in app code (notify → Notification,
dkb diagnostics → Notification, jobs backfill notice deleted, advisor
emit_rec_cards + graduation _surface_to_inbox deleted). Old rows are
deliberately NOT migrated — they die with the table (audit §4).

Revision ID: 0098_drop_review_items
Revises: 0097_notifications
"""

from alembic import op
import sqlalchemy as sa

revision = "0098_drop_review_items"
down_revision = "0097_notifications"
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
    if inspector.has_table("review_items"):
        op.drop_table("review_items")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("review_items"):
        return
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
