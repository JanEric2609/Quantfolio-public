"""Security master tables: securities, security_listings, security_aliases.

Stage 1 of DEC-B (full security master, staged) — remediation task T2.1 of
the 2026-08 universe/regime audit plan. ``securities`` holds the canonical
instrument record keyed by ISIN; ``security_listings`` carries per-venue
trading data (ISO 10383 MIC + symbol) so XETRA can be canonical per DEC-A;
``security_aliases`` is the permanent resolution cache keyed by
sha256(normalized alias)+source, making external identifier lookups
once-per-instrument-ever.

Revision ID: 0102_security_master_tables
Revises: 0100_ac_tuning_trials
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0102_security_master_tables"
down_revision = "0100_ac_tuning_trials"
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

    if not inspector.has_table("securities"):
        op.create_table(
            "securities",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("isin", sa.String(12), nullable=False),
            sa.Column("canonical_name", sa.String(240), nullable=False),
            sa.Column("asset_type", sa.String(24), nullable=False, server_default="stock"),
            sa.Column("base_currency", sa.String(3), nullable=False, server_default="EUR"),
            sa.Column("ucits", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("domicile_country", sa.String(80), nullable=True),
            sa.Column("ter", sa.Numeric(8, 4), nullable=True),
            sa.Column("provider_meta_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        safe_create_index("ix_securities_isin", "securities", ["isin"], unique=True)

    if not inspector.has_table("security_listings"):
        op.create_table(
            "security_listings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "security_id",
                sa.String(36),
                sa.ForeignKey("securities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("mic", sa.String(8), nullable=False),
            sa.Column("exchange_label", sa.String(32), nullable=False),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.UniqueConstraint("mic", "symbol", name="uq_security_listings_mic_symbol"),
        )
        safe_create_index("ix_security_listings_security_id", "security_listings", ["security_id"])

    if not inspector.has_table("security_aliases"):
        op.create_table(
            "security_aliases",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("alias_sha256", sa.String(64), nullable=False),
            sa.Column("source", sa.String(24), nullable=False),
            sa.Column("alias_value", sa.Text(), nullable=False),
            sa.Column(
                "security_id",
                sa.String(36),
                sa.ForeignKey("securities.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("source", "alias_sha256", name="uq_security_aliases_source_hash"),
        )
        safe_create_index("ix_security_aliases_alias_sha256", "security_aliases", ["alias_sha256"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("security_aliases"):
        op.drop_table("security_aliases")
    if inspector.has_table("security_listings"):
        op.drop_table("security_listings")
    if inspector.has_table("securities"):
        safe_drop_index("ix_securities_isin", table_name="securities")
        op.drop_table("securities")
