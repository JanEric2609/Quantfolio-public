"""broker_pies: Trading 212 pies as last read (read-only mirror).

Additive only: no existing table changes.

Revision ID: 0142_broker_pies
Revises: 0141_macro_drop_nan_rows
"""
from alembic import op
import sqlalchemy as sa

revision = "0142_broker_pies"
down_revision = "0141_macro_drop_nan_rows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("broker_pies"):
        return
    op.create_table(
        "broker_pies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "connected_account_id", sa.String(36),
            sa.ForeignKey("connected_accounts.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(40), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("cash", sa.Numeric(20, 6), nullable=True),
        sa.Column("invested", sa.Numeric(20, 6), nullable=True),
        sa.Column("value", sa.Numeric(20, 6), nullable=True),
        sa.Column("result", sa.Numeric(20, 6), nullable=True),
        sa.Column("result_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("dividends_gained", sa.Numeric(20, 6), nullable=True),
        sa.Column("dividends_reinvested", sa.Numeric(20, 6), nullable=True),
        sa.Column("dividends_in_cash", sa.Numeric(20, 6), nullable=True),
        sa.Column("dividend_action", sa.String(24), nullable=True),
        sa.Column("goal", sa.Numeric(20, 6), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("instruments_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "source", "external_id", name="uq_broker_pies_user_source_ext"),
    )
    op.create_index("ix_broker_pies_user_id", "broker_pies", ["user_id"])
    op.create_index("ix_broker_pies_connected_account_id", "broker_pies", ["connected_account_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("broker_pies"):
        op.drop_index("ix_broker_pies_connected_account_id", table_name="broker_pies")
        op.drop_index("ix_broker_pies_user_id", table_name="broker_pies")
        op.drop_table("broker_pies")
