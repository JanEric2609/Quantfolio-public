"""Plan 3: enrich q_values for persisted verification transitions

Revision ID: 0025_verification_loop_state
Revises: 0024_regime_macro_split
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0025_verification_loop_state"
down_revision = "0024_regime_macro_split"
branch_labels = None
depends_on = None


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns(table)}
    if column.name not in existing:
        op.add_column(table, column)


def _create_index_if_missing(table: str, index_name: str, columns: list[str]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes(table)}
    if index_name not in existing:
        safe_create_index(index_name, table, columns)


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
    _add_column_if_missing("q_values", sa.Column("reward", sa.Float(), nullable=True))
    _add_column_if_missing("q_values", sa.Column("next_state_hash", sa.String(length=64), nullable=True))
    _add_column_if_missing("q_values", sa.Column("done", sa.Boolean(), nullable=True, server_default=sa.false()))
    _add_column_if_missing("q_values", sa.Column("regime_label", sa.String(length=24), nullable=True))
    _add_column_if_missing("q_values", sa.Column("crisis", sa.Boolean(), nullable=True, server_default=sa.false()))
    _add_column_if_missing("q_values", sa.Column("state_json", sa.Text(), nullable=True))
    _add_column_if_missing("q_values", sa.Column("next_state_json", sa.Text(), nullable=True))
    _add_column_if_missing("q_values", sa.Column("checkpoint_json", sa.Text(), nullable=True))
    _add_column_if_missing("q_values", sa.Column("validation_score", sa.Float(), nullable=True))
    _add_column_if_missing("q_values", sa.Column("loss", sa.Float(), nullable=True))
    _create_index_if_missing("q_values", "ix_q_values_next_state_hash", ["next_state_hash"])
    _create_index_if_missing("q_values", "ix_q_values_regime_label", ["regime_label"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "q_values" not in inspector.get_table_names():
        return
    indexes = {idx["name"] for idx in inspector.get_indexes("q_values")}
    if "ix_q_values_regime_label" in indexes:
        safe_drop_index("ix_q_values_regime_label", table_name="q_values")
    if "ix_q_values_next_state_hash" in indexes:
        safe_drop_index("ix_q_values_next_state_hash", table_name="q_values")
    existing = {col["name"] for col in inspector.get_columns("q_values")}
    for column_name in (
        "loss",
        "validation_score",
        "checkpoint_json",
        "next_state_json",
        "state_json",
        "crisis",
        "regime_label",
        "done",
        "next_state_hash",
        "reward",
    ):
        if column_name in existing:
            op.drop_column("q_values", column_name)
