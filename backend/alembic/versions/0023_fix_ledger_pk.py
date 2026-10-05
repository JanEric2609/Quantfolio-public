"""Fix performance_ledger_entries PK to include partition column (TimescaleDB requirement)

Revision ID: 0023_fix_ledger_pk
Revises: 0022_phase8_indexes
Create Date: 2026-05-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0023_fix_ledger_pk"
down_revision = "0022_phase8_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # The (id, as_of) composite PK is a TimescaleDB partitioning requirement and relies on
    # ALTER-constraint DDL that SQLite cannot execute. It only matters on PostgreSQL, where
    # the table becomes a hypertable (see 0015_hypertables); skip elsewhere, matching the
    # dialect guards used across the other PostgreSQL-specific migrations.
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if inspector.has_table("performance_ledger_entries"):
        # A fresh install already gets (id, as_of) from 0017, under the naming
        # convention's name; only an older database carries the id-only PK.
        pk = inspector.get_pk_constraint("performance_ledger_entries")
        if pk["constrained_columns"] == ["id", "as_of"]:
            return
        # Drop the existing primary key constraint
        op.drop_constraint(
            pk["name"],
            "performance_ledger_entries",
            type_="primary",
        )
        # Re-create with (id, as_of) so TimescaleDB can partition on as_of
        op.create_primary_key(
            "performance_ledger_entries_pkey",
            "performance_ledger_entries",
            ["id", "as_of"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if inspector.has_table("performance_ledger_entries"):
        op.drop_constraint(
            inspector.get_pk_constraint("performance_ledger_entries")["name"],
            "performance_ledger_entries",
            type_="primary",
        )
        op.create_primary_key(
            "performance_ledger_entries_pkey",
            "performance_ledger_entries",
            ["id"],
        )
