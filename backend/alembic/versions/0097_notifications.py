"""Add notifications table — dedicated in-app bell notifications (plan todo 7).

Re-homes bell notifications off ``review_items`` (audit §4) onto a dedicated
table ahead of the full review-inbox removal (todo 8 / migration 0098).
Old review_items rows are deliberately NOT migrated.

Revision ID: 0097_notifications
Revises: 0096_ac_trial_ledger
"""

from alembic import op
import sqlalchemy as sa

revision = "0097_notifications"
down_revision = "0096_ac_trial_ledger"
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
    if not inspector.has_table("notifications"):
        op.create_table(
            "notifications",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
            sa.Column("href", sa.String(512), nullable=True),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        safe_create_index("ix_notifications_user_id", "notifications", ["user_id"])
        safe_create_index("ix_notifications_source", "notifications", ["source"])
        safe_create_index(
            "ix_notifications_user_created", "notifications", ["user_id", "created_at"]
        )
        safe_create_index(
            "ix_notifications_user_unread", "notifications", ["user_id", "read_at"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("notifications"):
        return
    indexes = {ix["name"] for ix in inspector.get_indexes("notifications")}
    for name in (
        "ix_notifications_user_unread",
        "ix_notifications_user_created",
        "ix_notifications_source",
        "ix_notifications_user_id",
    ):
        if name in indexes:
            safe_drop_index(name, table_name="notifications")
    op.drop_table("notifications")
