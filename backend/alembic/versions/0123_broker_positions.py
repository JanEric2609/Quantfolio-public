"""Generic broker tables: broker_positions and broker_sync_logs.

Scalable Capital (read-only, through the official ``sc`` CLI) is the first
broker to use them. Additive only: no existing table changes.

Revision ID: 0123_broker_positions
Revises: 0122_snapshot_portfolio_backfill
"""
from alembic import op
import sqlalchemy as sa

revision = "0123_broker_positions"
down_revision = "0122_snapshot_portfolio_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("broker_positions"):
        op.create_table(
            "broker_positions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "connected_account_id",
                sa.String(36),
                sa.ForeignKey("connected_accounts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("isin", sa.String(12), nullable=False),
            sa.Column("ticker", sa.String(32), nullable=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("security_type", sa.String(32), nullable=True),
            sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
            sa.Column("pending_quantity", sa.Numeric(20, 8), nullable=True),
            sa.Column("avg_buy_price", sa.Numeric(20, 6), nullable=True),
            sa.Column("current_price", sa.Numeric(20, 6), nullable=True),
            sa.Column("current_value", sa.Numeric(20, 6), nullable=True),
            sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
            sa.Column("price_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("price_outdated", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_synced", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("connected_account_id", "isin", name="uq_broker_positions_account_isin"),
        )
        op.create_index("ix_broker_positions_user_id", "broker_positions", ["user_id"])
        op.create_index("ix_broker_positions_connected_account_id", "broker_positions", ["connected_account_id"])
        op.create_index("ix_broker_positions_source", "broker_positions", ["source"])
        op.create_index("ix_broker_positions_isin", "broker_positions", ["isin"])
        op.create_index("ix_broker_positions_ticker", "broker_positions", ["ticker"])
        op.create_index("ix_broker_positions_user_source", "broker_positions", ["user_id", "source"])

    if not inspector.has_table("broker_sync_logs"):
        op.create_table(
            "broker_sync_logs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("trigger", sa.String(16), nullable=False, server_default="manual"),
            sa.Column("state", sa.String(16), nullable=False, server_default="running"),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("message", sa.Text(), nullable=False, server_default=""),
            sa.Column("counts_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_broker_sync_logs_user_id", "broker_sync_logs", ["user_id"])
        op.create_index(
            "ix_broker_sync_logs_user_source_started",
            "broker_sync_logs",
            ["user_id", "source", "started_at"],
        )
    existing = {ix["name"] for ix in sa.inspect(bind).get_indexes("broker_sync_logs")}
    if "uq_broker_sync_logs_one_running" not in existing:
        # One running sync per broker (the service maps the violation to "busy").
        op.create_index(
            "uq_broker_sync_logs_one_running",
            "broker_sync_logs",
            ["source"],
            unique=True,
            sqlite_where=sa.text("state = 'running'"),
            postgresql_where=sa.text("state = 'running'"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("broker_sync_logs"):
        op.drop_table("broker_sync_logs")
    if inspector.has_table("broker_positions"):
        op.drop_table("broker_positions")
