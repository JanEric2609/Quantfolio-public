"""book_position_snapshots: daily positions at every broker.

position_snapshots holds DKB only (FK to dkb_accounts). The book's
time-weighted return needs the holdings of every broker, so this table keeps
one row per (user, day, broker, account, ISIN), and is back-filled from the
DKB rows already recorded.

Revision ID: 0127_book_position_snapshots
Revises: 0126_tax_event_bank
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0127_book_position_snapshots"
down_revision = "0126_tax_event_bank"
branch_labels = None
depends_on = None

_TABLE = "book_position_snapshots"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("snapshot_date", sa.Date(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("account_id", sa.String(length=36), nullable=False),
            sa.Column("isin", sa.String(length=12), nullable=False),
            sa.Column("ticker", sa.String(length=32), nullable=True),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
            sa.Column("current_price", sa.Numeric(20, 6), nullable=True),
            sa.Column("current_value", sa.Numeric(20, 6), nullable=True),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "snapshot_date", "source", "account_id", "isin",
                                name="uq_book_position_snapshot"),
        )
        op.create_index("ix_book_position_snapshots_user_id", _TABLE, ["user_id"])
        op.create_index("ix_book_position_snapshots_user_date", _TABLE, ["user_id", "snapshot_date"])

    if not inspector.has_table("position_snapshots") or not inspector.has_table("dkb_accounts"):
        return
    # Back-fill DKB history, one row per account, day and ISIN (the old table
    # has no unique key; the latest row of a day wins).
    rows = bind.execute(sa.text(
        "SELECT a.user_id, ps.snapshot_date, ps.account_id, ps.isin, ps.ticker, ps.name, ps.quantity, "
        "ps.current_price, ps.current_value, ps.created_at "
        "FROM position_snapshots ps JOIN dkb_accounts a ON a.id = ps.account_id "
        "ORDER BY ps.created_at"
    )).fetchall()
    latest: dict[tuple, tuple] = {}
    for r in rows:
        latest[(r[0], str(r[1]), r[2], r[3])] = r
    existing = {
        (u, str(d), a, i)
        for u, d, a, i in bind.execute(sa.text(
            f"SELECT user_id, snapshot_date, account_id, isin FROM {_TABLE} WHERE source = 'dkb'"
        )).fetchall()
    }
    now = datetime.now(UTC)
    insert = sa.text(
        f"INSERT INTO {_TABLE} (id, user_id, snapshot_date, source, account_id, isin, ticker, name, quantity, "
        "current_price, current_value, currency, created_at) VALUES (:id, :user_id, :snapshot_date, 'dkb', "
        ":account_id, :isin, :ticker, :name, :quantity, :current_price, :current_value, 'EUR', :created_at)"
    )
    for key, r in latest.items():
        if key in existing:
            continue
        bind.execute(insert, {
            "id": str(uuid.uuid4()), "user_id": r[0], "snapshot_date": r[1], "account_id": r[2],
            "isin": r[3], "ticker": r[4], "name": r[5] or r[3], "quantity": r[6], "current_price": r[7],
            "current_value": r[8], "created_at": r[9] or now,
        })


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        op.drop_index("ix_book_position_snapshots_user_date", table_name=_TABLE)
        op.drop_index("ix_book_position_snapshots_user_id", table_name=_TABLE)
        op.drop_table(_TABLE)
