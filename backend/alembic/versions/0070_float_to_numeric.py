"""Convert precision-critical Float columns to Numeric for monetary calculation accuracy.

- recommendation_outcomes: abs_return, benchmark_excess_return -> Numeric(12,6)
- alpha_signals: value -> Numeric(12,6)
- Skips ML/score columns (metrics, percentages, correlations, z-scores).

Revision ID: 0070_float_to_numeric
Revises: 0069_add_missing_indexes
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa


revision = "0070_float_to_numeric"
down_revision = "0069_add_missing_indexes"
branch_labels = None
depends_on = None


def _alter_float_to_numeric(table: str, column: str, precision: int = 12, scale: int = 6) -> None:
    """Convert a Float column to Numeric if the table and column exist.
    
    Uses batch mode for SQLite compatibility (batch_alter_table is a no-op on PostgreSQL).
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return
    columns = {c["name"] for c in inspector.get_columns(table)}
    if column not in columns:
        return
    # Float type guard — idempotent: skip if already converted
    col_info = next(c for c in inspector.get_columns(table) if c["name"] == column)
    type_str = str(col_info["type"]).upper() if col_info["type"] else ""
    if "FLOAT" not in type_str and "REAL" not in type_str:
        return

    # Cleanse NaN/Infinity values that would abort the Numeric conversion
    # NaN is the only value not equal to itself; infinity is caught by the range check.
    bind.execute(
        sa.text(
            f"UPDATE {table} SET {column} = NULL "
            f"WHERE {column} IS NOT NULL AND ({column} != {column} OR abs({column}) > 1e100)"
        )
    )

    with op.batch_alter_table(table) as batch_op:
        batch_op.alter_column(
            column,
            type_=sa.Numeric(precision, scale),
            existing_type=sa.Float(),
        )


def upgrade() -> None:
    # recommendation_outcomes: monetary return values
    _alter_float_to_numeric("recommendation_outcomes", "abs_return", precision=12, scale=6)
    _alter_float_to_numeric("recommendation_outcomes", "benchmark_excess_return", precision=12, scale=6)

    # alpha_signals: signal value scores (precision-critical)
    _alter_float_to_numeric("alpha_signals", "value", precision=12, scale=6)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def _revert_numeric_to_float(table: str, column: str) -> None:
        if not inspector.has_table(table):
            return
        cols = {c["name"] for c in inspector.get_columns(table)}
        if column not in cols:
            return
        col_info = next(c for c in inspector.get_columns(table) if c["name"] == column)
        col_type = str(col_info["type"]).upper() if col_info["type"] else ""
        if "NUMERIC" not in col_type and "DECIMAL" not in col_type:
            return
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                column,
                type_=sa.Float(),
                existing_type=sa.Numeric(),
            )

    _revert_numeric_to_float("recommendation_outcomes", "abs_return")
    _revert_numeric_to_float("recommendation_outcomes", "benchmark_excess_return")
    _revert_numeric_to_float("alpha_signals", "value")
