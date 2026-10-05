"""ORM model repairs: money columns Float->Numeric, DkbAccount user_id index.

Revision ID: 0027_review_repairs
Revises: 0026_user_session_version
Create Date: 2026-06-02

Notes
-----
- bar_prices / factor_loadings_daily / regime_snapshots / provider_health_history
  are ALL present after 0015_hypertables on a fresh DB — no repair needed.
- UUID-vs-String(36) reconciliation is intentionally DEFERRED (high-risk, separate task).
- FK ondelete changes (category FKs SET NULL, DkbAccount.user_id CASCADE) are ORM-only;
  SQLite does not enforce FK actions at the DDL level, so no migration DDL is needed for
  existing SQLite databases. Postgres FK ondelete is handled by recreating constraints,
  which is a separate, high-risk operation deferred to a future migration.
"""

from alembic import op
import sqlalchemy as sa


revision = "0027_review_repairs"
down_revision = "0026_user_session_version"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_column_type(inspector, table: str, column: str) -> str | None:
    """Return the string type name for a column, or None if column absent."""
    for col in inspector.get_columns(table):
        if col["name"] == column:
            return type(col["type"]).__name__.upper()
    return None


def _index_exists(inspector, table: str, index_name: str) -> bool:
    return any(ix["name"] == index_name for ix in inspector.get_indexes(table))


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 1. ix_dkb_accounts_user_id — missing on existing databases
    # ------------------------------------------------------------------
    if inspector.has_table("dkb_accounts") and not _index_exists(inspector, "dkb_accounts", "ix_dkb_accounts_user_id"):
        safe_create_index("ix_dkb_accounts_user_id", "dkb_accounts", ["user_id"])

    # ------------------------------------------------------------------
    # 2. goals.target_amount  Float -> Numeric(20,6)
    # ------------------------------------------------------------------
    if inspector.has_table("goals"):
        col_type = _get_column_type(inspector, "goals", "target_amount")
        if col_type and col_type == "FLOAT":
            with op.batch_alter_table("goals", recreate="always") as batch_op:
                batch_op.alter_column(
                    "target_amount",
                    type_=sa.Numeric(20, 6),
                    existing_type=sa.Float(),
                    existing_nullable=True,
                )

    # ------------------------------------------------------------------
    # 3. goals.progress  Float -> Numeric(20,6)
    # ------------------------------------------------------------------
    if inspector.has_table("goals"):
        col_type = _get_column_type(inspector, "goals", "progress")
        if col_type and col_type == "FLOAT":
            with op.batch_alter_table("goals", recreate="always") as batch_op:
                batch_op.alter_column(
                    "progress",
                    type_=sa.Numeric(20, 6),
                    existing_type=sa.Float(),
                    existing_nullable=False,
                )

    # ------------------------------------------------------------------
    # 4. performance_ledger_entries.twr/mwr/dispersion  Float -> Numeric(20,6)
    # ------------------------------------------------------------------
    if inspector.has_table("performance_ledger_entries"):
        twr_type = _get_column_type(inspector, "performance_ledger_entries", "twr")
        if twr_type and twr_type == "FLOAT":
            with op.batch_alter_table("performance_ledger_entries", recreate="always") as batch_op:
                batch_op.alter_column(
                    "twr",
                    type_=sa.Numeric(20, 6),
                    existing_type=sa.Float(),
                    existing_nullable=False,
                )
                batch_op.alter_column(
                    "mwr",
                    type_=sa.Numeric(20, 6),
                    existing_type=sa.Float(),
                    existing_nullable=False,
                )
                batch_op.alter_column(
                    "dispersion",
                    type_=sa.Numeric(20, 6),
                    existing_type=sa.Float(),
                    existing_nullable=True,
                )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ------------------------------------------------------------------
    # 4. performance_ledger_entries.twr/mwr/dispersion  Numeric -> Float
    # ------------------------------------------------------------------
    if inspector.has_table("performance_ledger_entries"):
        twr_type = _get_column_type(inspector, "performance_ledger_entries", "twr")
        if twr_type and twr_type in ("NUMERIC", "DECIMAL"):
            with op.batch_alter_table("performance_ledger_entries", recreate="always") as batch_op:
                batch_op.alter_column(
                    "twr",
                    type_=sa.Float(),
                    existing_type=sa.Numeric(20, 6),
                    existing_nullable=False,
                )
                batch_op.alter_column(
                    "mwr",
                    type_=sa.Float(),
                    existing_type=sa.Numeric(20, 6),
                    existing_nullable=False,
                )
                batch_op.alter_column(
                    "dispersion",
                    type_=sa.Float(),
                    existing_type=sa.Numeric(20, 6),
                    existing_nullable=True,
                )

    # ------------------------------------------------------------------
    # 3. goals.progress  Numeric -> Float
    # ------------------------------------------------------------------
    if inspector.has_table("goals"):
        col_type = _get_column_type(inspector, "goals", "progress")
        if col_type and col_type in ("NUMERIC", "DECIMAL"):
            with op.batch_alter_table("goals", recreate="always") as batch_op:
                batch_op.alter_column(
                    "progress",
                    type_=sa.Float(),
                    existing_type=sa.Numeric(20, 6),
                    existing_nullable=False,
                )

    # ------------------------------------------------------------------
    # 2. goals.target_amount  Numeric -> Float
    # ------------------------------------------------------------------
    if inspector.has_table("goals"):
        col_type = _get_column_type(inspector, "goals", "target_amount")
        if col_type and col_type in ("NUMERIC", "DECIMAL"):
            with op.batch_alter_table("goals", recreate="always") as batch_op:
                batch_op.alter_column(
                    "target_amount",
                    type_=sa.Float(),
                    existing_type=sa.Numeric(20, 6),
                    existing_nullable=True,
                )

    # ------------------------------------------------------------------
    # 1. Drop ix_dkb_accounts_user_id
    # ------------------------------------------------------------------
    if inspector.has_table("dkb_accounts") and _index_exists(inspector, "dkb_accounts", "ix_dkb_accounts_user_id"):
        safe_drop_index("ix_dkb_accounts_user_id", table_name="dkb_accounts")
