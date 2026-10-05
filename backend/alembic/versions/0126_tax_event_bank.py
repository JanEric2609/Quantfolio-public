"""tax_ledger_events.institution: which bank booked the event.

Each bank applies only its own Freistellungsauftrag (Sec. 44a EStG), so the tax
cockpit needs to know where a dividend, interest payment or sale happened.
Synced rows are backfilled from their source (``dkb_sync`` -> ``dkb``,
``scalable_sync`` -> ``scalable``); manual rows stay NULL until edited.

Revision ID: 0126_tax_event_bank
Revises: 0125_admin_sole_user
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0126_tax_event_bank"
down_revision = "0125_admin_sole_user"
branch_labels = None
depends_on = None

_TABLE = "tax_ledger_events"
_INDEX = "ix_tax_ledger_user_institution"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    if "institution" not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column("institution", sa.String(length=32), nullable=True))
    if _INDEX not in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.create_index(_INDEX, _TABLE, ["user_id", "institution"])
    bind.execute(sa.text(
        "UPDATE tax_ledger_events SET institution = 'dkb' WHERE institution IS NULL AND source = 'dkb_sync'"
    ))
    bind.execute(sa.text(
        "UPDATE tax_ledger_events SET institution = 'scalable' "
        "WHERE institution IS NULL AND source = 'scalable_sync'"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(_TABLE):
        return
    if _INDEX in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
    if "institution" in {c["name"] for c in inspector.get_columns(_TABLE)}:
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column("institution")
