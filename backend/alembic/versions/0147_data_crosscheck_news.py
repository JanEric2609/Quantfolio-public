"""Second-source price checks, 8-K events and GDELT news mentions (plan 2026-11-09).

* ``price_cross_checks``: a stored series compared with a second provider.
* ``sec_8k_events``: 8-K filings of universe members with their item codes.
* ``news_mentions``: GDELT articles matched to universe members.
* ``news_tone_scores.source``: where a night's headlines came from.

Revision ID: 0147_data_crosscheck_news
Revises: 0146_forward_lab
"""
from alembic import op
import sqlalchemy as sa

revision = "0147_data_crosscheck_news"
down_revision = "0146_forward_lab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("price_cross_checks"):
        op.create_table(
            "price_cross_checks",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("checked_on", sa.Date(), nullable=False),
            sa.Column("reference", sa.String(24), nullable=False),
            sa.Column("n_days", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_return_diff", sa.Float(), nullable=True),
            sa.Column("verdict", sa.String(16), nullable=False),
            sa.Column("pattern", sa.String(24), nullable=True),
            sa.Column("action", sa.String(24), nullable=False, server_default="none"),
            sa.Column("detail_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("symbol", "checked_on", "reference", name="uq_price_cross_check"),
        )
        op.create_index("ix_price_cross_checks_symbol", "price_cross_checks", ["symbol"])
        op.create_index("ix_price_cross_checks_checked_on", "price_cross_checks", ["checked_on"])
        op.create_index("ix_price_cross_checks_verdict", "price_cross_checks", ["verdict"])
    if not inspector.has_table("sec_8k_events"):
        op.create_table(
            "sec_8k_events",
            sa.Column("accession", sa.String(24), primary_key=True),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("cik", sa.String(10), nullable=False),
            sa.Column("form", sa.String(8), nullable=False),
            sa.Column("filed_on", sa.Date(), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("items", sa.String(120), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_sec_8k_events_symbol", "sec_8k_events", ["symbol"])
        op.create_index("ix_sec_8k_events_cik", "sec_8k_events", ["cik"])
        op.create_index("ix_sec_8k_events_filed_on", "sec_8k_events", ["filed_on"])
    if not inspector.has_table("news_mentions"):
        op.create_table(
            "news_mentions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source", sa.String(16), nullable=False, server_default="gdelt"),
            sa.Column("outlet", sa.String(120), nullable=True),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("title_key", sa.String(200), nullable=False),
            sa.Column("gdelt_tone", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("symbol", "day", "title_key", name="uq_news_mention"),
        )
        op.create_index("ix_news_mentions_symbol", "news_mentions", ["symbol"])
        op.create_index("ix_news_mentions_day", "news_mentions", ["day"])
    if inspector.has_table("news_tone_scores"):
        columns = {c["name"] for c in inspector.get_columns("news_tone_scores")}
        if "source" not in columns:
            op.add_column("news_tone_scores", sa.Column("source", sa.String(16), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("news_tone_scores"):
        columns = {c["name"] for c in inspector.get_columns("news_tone_scores")}
        if "source" in columns:
            with op.batch_alter_table("news_tone_scores") as batch:
                batch.drop_column("source")
    for table in ("news_mentions", "sec_8k_events", "price_cross_checks"):
        if inspector.has_table(table):
            op.drop_table(table)
