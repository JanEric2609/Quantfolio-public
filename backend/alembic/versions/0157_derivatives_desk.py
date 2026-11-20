"""desk_events (hash-chained), desk_positions, desk_registrations, news_wire_items (ADR 0027).

Additive only. On PostgreSQL a trigger refuses UPDATE and DELETE on desk_events.

Revision ID: 0157_derivatives_desk
Revises: 0156_derivatives
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0157_derivatives_desk"
down_revision = "0156_derivatives"
branch_labels = None
depends_on = None

_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION desk_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'desk_events is append-only';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("desk_events"):
        op.create_table(
            "desk_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("mode", sa.String(8), nullable=False, server_default="paper"),
            sa.Column("position_id", sa.String(36), nullable=True),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("prev_hash", sa.String(64), nullable=False),
            sa.Column("row_hash", sa.String(64), nullable=False),
            sa.UniqueConstraint("seq", name="uq_desk_events_seq"),
            sa.UniqueConstraint("prev_hash", name="uq_desk_events_prev_hash"),
        )
        op.create_index("ix_desk_events_position_id", "desk_events", ["position_id"])
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(_TRIGGER_FN))
        bind.execute(sa.text("DROP TRIGGER IF EXISTS desk_events_no_change ON desk_events"))
        bind.execute(sa.text(
            "CREATE TRIGGER desk_events_no_change BEFORE UPDATE OR DELETE ON desk_events "
            "FOR EACH ROW EXECUTE FUNCTION desk_events_append_only()"
        ))
    if not inspector.has_table("desk_positions"):
        op.create_table(
            "desk_positions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("mode", sa.String(8), nullable=False, server_default="paper"),
            sa.Column("cell", sa.String(48), nullable=False),
            sa.Column("signal", sa.String(24), nullable=False),
            sa.Column("product_type", sa.String(12), nullable=False),
            sa.Column("isin", sa.String(12), nullable=False),
            sa.Column("issuer", sa.String(32), nullable=True),
            sa.Column("underlying_isin", sa.String(12), nullable=False),
            sa.Column("underlying_label", sa.String(64), nullable=False, server_default=""),
            sa.Column("direction", sa.String(5), nullable=False),
            sa.Column("liquid", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("leverage_or_omega", sa.Numeric(20, 6), nullable=True),
            sa.Column("units", sa.Integer(), nullable=True),
            sa.Column("entry_price", sa.Numeric(20, 6), nullable=True),
            sa.Column("entry_fee", sa.Numeric(12, 4), nullable=True),
            sa.Column("entry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("stop_price", sa.Numeric(20, 6), nullable=True),
            sa.Column("max_exit_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("exit_price", sa.Numeric(20, 6), nullable=True),
            sa.Column("exit_fee", sa.Numeric(12, 4), nullable=True),
            sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("exit_reason", sa.String(24), nullable=True),
            sa.Column("net_eur", sa.Numeric(14, 4), nullable=True),
            sa.Column("spread_pct", sa.Numeric(10, 6), nullable=True),
            sa.Column("underlying_entry", sa.Numeric(20, 6), nullable=True),
            sa.Column("last_bid", sa.Numeric(20, 6), nullable=True),
            sa.Column("last_quote_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("underlying_move", sa.Numeric(14, 8), nullable=True),
            sa.Column("product_name", sa.String(120), nullable=True),
            sa.Column("wkn", sa.String(6), nullable=True),
            sa.Column("barrier_or_strike", sa.Numeric(20, 6), nullable=True),
            sa.Column("expiry", sa.Date(), nullable=True),
            sa.Column("quantity", sa.Numeric(20, 8), nullable=True),
            sa.Column("status", sa.String(10), nullable=False, server_default="proposed"),
            sa.Column("would_take_live", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("prereg_hash", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_desk_positions_user_id", "desk_positions", ["user_id"])
        op.create_index("ix_desk_positions_cell", "desk_positions", ["cell"])
        op.create_index("ix_desk_positions_status", "desk_positions", ["status"])
        op.create_index(
            "uq_desk_positions_one_live_open", "desk_positions", ["mode"], unique=True,
            postgresql_where=sa.text("mode = 'live' AND status = 'open'"),
            sqlite_where=sa.text("mode = 'live' AND status = 'open'"),
        )
    if not inspector.has_table("desk_registrations"):
        op.create_table(
            "desk_registrations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("prereg_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("spec", sa.Text(), nullable=False),
            sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        )
    if not inspector.has_table("news_wire_items"):
        op.create_table(
            "news_wire_items",
            sa.Column("id", sa.String(96), primary_key=True),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("headline", sa.String(400), nullable=False),
            sa.Column("symbols", sa.String(200), nullable=False, server_default=""),
            sa.Column("url", sa.String(400), nullable=True),
            sa.Column("relevance", sa.String(64), nullable=False, server_default=""),
            sa.Column("materiality", sa.Integer(), nullable=True),
            sa.Column("direction", sa.String(8), nullable=True),
            sa.Column("scope", sa.String(8), nullable=True),
            sa.Column("rated_ms", sa.Integer(), nullable=True),
            sa.Column("alert", sa.String(12), nullable=False, server_default=""),
        )
        op.create_index("ix_news_wire_items_source", "news_wire_items", ["source"])
        op.create_index("ix_news_wire_items_received_at", "news_wire_items", ["received_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("DROP TRIGGER IF EXISTS desk_events_no_change ON desk_events"))
        bind.execute(sa.text("DROP FUNCTION IF EXISTS desk_events_append_only()"))
    for table in ("news_wire_items", "desk_registrations", "desk_positions", "desk_events"):
        if inspector.has_table(table):
            op.drop_table(table)
