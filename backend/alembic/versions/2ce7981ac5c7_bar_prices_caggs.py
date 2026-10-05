"""bar_prices continuous aggregates + retention policy

Split out of 2ce7981ac5c6_recreate_analytics only to keep the two logical
concerns (tables vs. continuous aggregates) in their own migration files. The
CAGGs are still created in the *same* Alembic transaction as ``bar_prices``
(``env.py`` wraps the whole upgrade in one ``begin_transaction``). That is
intentional and correct: a continuous aggregate created here can see
``bar_prices`` because it was created earlier in the same transaction. We must
NOT flip the DBAPI connection to autocommit — doing so runs the CAGG DDL in its
own transaction snapshot, which cannot see the not-yet-committed ``bar_prices``
and raises ``relation "bar_prices" does not exist``. TimescaleDB supports
transactional DDL, so plain ``bind.execute(sa.text(...))`` is the right call.

Revision ID: 2ce7981ac5c7_bar_prices_caggs
Revises: 2ce7981ac5c6_recreate_analytics
Create Date: 2026-08-29 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2ce7981ac5c7_bar_prices_caggs"
down_revision: Union[str, Sequence[str], None] = "2ce7981ac5c6_recreate_analytics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: (re)create bar_prices continuous aggregates + retention.

    Runs on the same Alembic connection/transaction as the table-creating
    migration. A continuous aggregate created here can see ``bar_prices`` because
    it was created earlier in the *same* transaction. We do NOT flip the DBAPI
    connection to autocommit — that would run the CAGG DDL in its own snapshot and
    hide the not-yet-committed table (``relation "bar_prices" does not exist``).
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if not inspector.has_table("bar_prices"):
        # The owning migration creates the table; if it is missing something is
        # broken upstream. Skipping here avoids masking that failure.
        return
    bind.execute(
        sa.text(
            """
            CREATE MATERIALIZED VIEW IF NOT EXISTS bar_prices_weekly
            WITH (timescaledb.continuous) AS
            SELECT
                symbol,
                time_bucket('7 days', ts) AS week_ts,
                first(open, ts) AS open,
                max(high) AS high,
                min(low) AS low,
                last(close, ts) AS close,
                sum(volume) AS volume,
                currency,
                last(provider, ts) AS provider
            FROM bar_prices
            GROUP BY symbol, week_ts, currency
            WITH NO DATA;
            """
        )
    )
    bind.execute(
        sa.text(
            """
            CREATE MATERIALIZED VIEW IF NOT EXISTS bar_prices_monthly
            WITH (timescaledb.continuous) AS
            SELECT
                symbol,
                time_bucket('30 days', ts) AS month_ts,
                first(open, ts) AS open,
                max(high) AS high,
                min(low) AS low,
                last(close, ts) AS close,
                sum(volume) AS volume,
                currency,
                last(provider, ts) AS provider
            FROM bar_prices
            GROUP BY symbol, month_ts, currency
            WITH NO DATA;
            """
        )
    )
    # NOTE: refresh_continuous_aggregate() and add_retention_policy() require
    # autocommit and cannot run inside the Alembic transaction block.
    # They are handled by the price-backfill script and background jobs.


def downgrade() -> None:
    """Downgrade schema: drop the continuous aggregates.

    Mirrors ``upgrade()`` exactly: dialect-guarded, and executed directly on the
    Alembic connection. ``bind.begin()`` must NOT be used here — env.py has
    already opened a transaction on this connection, so beginning another raises
    ``InvalidRequestError: This connection has already initialized a SQLAlchemy
    Transaction()``. Statements must also be wrapped in ``sa.text()``; SQLAlchemy
    2.0 refuses to execute bare strings.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text("DROP MATERIALIZED VIEW IF EXISTS bar_prices_monthly CASCADE"))
    bind.execute(sa.text("DROP MATERIALIZED VIEW IF EXISTS bar_prices_weekly CASCADE"))
