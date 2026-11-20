"""add conversation_id to chat_messages

Revision ID: 0011_chat_conversation_id
Revises: 0010_quant_rl_policies
Create Date: 2026-05-21

"""

import sqlalchemy as sa
from alembic import op

revision = "0011_chat_conversation_id"
down_revision = "0010_quant_rl_policies"


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
    if inspector.has_table("chat_messages"):
        columns = [c["name"] for c in inspector.get_columns("chat_messages")]
        if "conversation_id" not in columns:
            op.add_column("chat_messages", sa.Column("conversation_id", sa.String(36), nullable=True))
        existing_indexes = [ix["name"] for ix in inspector.get_indexes("chat_messages")]
        if "ix_chat_messages_conversation_id" in existing_indexes:
            safe_drop_index("ix_chat_messages_conversation_id", table_name="chat_messages")
        if "ix_chat_messages_user_conv" not in existing_indexes:
            safe_create_index("ix_chat_messages_user_conv", "chat_messages", ["user_id", "conversation_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("chat_messages"):
        columns = [c["name"] for c in inspector.get_columns("chat_messages")]
        if "conversation_id" in columns:
            op.drop_column("chat_messages", "conversation_id")
