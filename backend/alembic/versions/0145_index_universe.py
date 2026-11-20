"""MSCI World constituents for the Discover universe; analyst-target cache; larger cap.

* ``index_universe_members``: one row per constituent of an index (MSCI World
  from the SPDR holdings file), its ISIN, GICS sector and the Yahoo symbol
  OpenFIGI mapped it to; ``symbol_ok`` is cleared when Yahoo has no prices.
* ``analyst_consensus_cache``: the last price-target consensus per symbol,
  so a 1,500-name run does not ask Yahoo for every name every time.
* The system universe config's ``universe_max_size`` goes from 300 to 1800
  (only when it still holds the old system default; an owner's edit stays).

Revision ID: 0145_index_universe
Revises: 0144_trading212_switch_on
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "0145_index_universe"
down_revision = "0144_trading212_switch_on"
branch_labels = None
depends_on = None

OLD_MAX, NEW_MAX = 300, 1800


def _set_universe_max(bind, old: int, new: int) -> None:
    rows = bind.execute(sa.text(
        "SELECT id, config_json FROM discovery_config "
        "WHERE config_type = 'universe' AND status = 'active' AND source = 'system'"
    )).fetchall()
    for row_id, raw in rows:
        cfg = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        if cfg.get("universe_max_size") != old:
            continue
        cfg["universe_max_size"] = new
        bind.execute(
            sa.text("UPDATE discovery_config SET config_json = :cfg WHERE id = :id"),
            {"cfg": json.dumps(cfg), "id": row_id},
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("index_universe_members"):
        op.create_table(
            "index_universe_members",
            sa.Column("index_key", sa.String(32), primary_key=True),
            sa.Column("isin", sa.String(12), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("country", sa.String(64), nullable=True),
            sa.Column("currency", sa.String(3), nullable=True),
            sa.Column("sector", sa.String(64), nullable=True),
            sa.Column("industry", sa.String(128), nullable=True),
            sa.Column("weight", sa.Float(), nullable=True),
            sa.Column("as_of", sa.Date(), nullable=True),
            sa.Column("symbol", sa.String(32), nullable=True),
            sa.Column("symbol_source", sa.String(32), nullable=True),
            sa.Column("symbol_ok", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("mapped_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_index_universe_members_symbol", "index_universe_members", ["symbol"])
    if not inspector.has_table("analyst_consensus_cache"):
        op.create_table(
            "analyst_consensus_cache",
            sa.Column("symbol", sa.String(32), primary_key=True),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("ok", sa.Boolean(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        )
    if inspector.has_table("discovery_config"):
        _set_universe_max(bind, OLD_MAX, NEW_MAX)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("discovery_config"):
        _set_universe_max(bind, NEW_MAX, OLD_MAX)
    if inspector.has_table("analyst_consensus_cache"):
        op.drop_table("analyst_consensus_cache")
    if inspector.has_table("index_universe_members"):
        op.drop_index("ix_index_universe_members_symbol", table_name="index_universe_members")
        op.drop_table("index_universe_members")
