"""Add QuantMlModel.ticker — needed to look up the latest validated model
for a given symbol (Track D1: discover's stage_ml_signal reads it).

QuantMlModel was trained against a ticker (supplied as a per-train request
param) but never persisted which one, so nothing could later ask "is there
a validated model for AAPL?" without external bookkeeping.

Revision ID: 0103_quant_ml_model_ticker
Revises: 0102_security_master_tables
"""

from alembic import op
import sqlalchemy as sa

revision = "0103_quant_ml_model_ticker"
down_revision = "0102_security_master_tables"
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
    if not inspector.has_table("quant_ml_models"):
        return
    columns = {col["name"] for col in inspector.get_columns("quant_ml_models")}
    if "ticker" not in columns:
        op.add_column("quant_ml_models", sa.Column("ticker", sa.String(32), nullable=True))
        safe_create_index(
            "ix_quant_ml_models_ticker", "quant_ml_models", ["ticker"], unique=False
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("quant_ml_models"):
        return
    columns = {col["name"] for col in inspector.get_columns("quant_ml_models")}
    if "ticker" in columns:
        safe_drop_index("ix_quant_ml_models_ticker", table_name="quant_ml_models")
        op.drop_column("quant_ml_models", "ticker")
