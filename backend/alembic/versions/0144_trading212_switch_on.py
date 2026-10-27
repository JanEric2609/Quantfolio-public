"""Switch Trading 212 on where its API key is already saved.

Saving the key used to leave ``trading212_enabled`` off, so the sync refused
to run and the UI hid the broker. Saving a credential now switches its
connection on; this does the same for a key saved before that change.

Revision ID: 0144_trading212_switch_on
Revises: 0143_broker_instruments
"""
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0144_trading212_switch_on"
down_revision = "0143_broker_instruments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not (inspector.has_table("api_keys") and inspector.has_table("app_settings")):
        return
    has_key = bind.execute(sa.text("SELECT 1 FROM api_keys WHERE service = 'trading212'")).first()
    if has_key is None:
        return
    row = bind.execute(sa.text("SELECT id FROM app_settings WHERE key = 'trading212_enabled'")).first()
    if row is None:
        bind.execute(
            sa.text("INSERT INTO app_settings (id, key, value_json, updated_at) VALUES (:id, 'trading212_enabled', 'true', CURRENT_TIMESTAMP)"),
            {"id": str(uuid.uuid4())},
        )
    else:
        bind.execute(sa.text("UPDATE app_settings SET value_json = 'true', updated_at = CURRENT_TIMESTAMP WHERE key = 'trading212_enabled'"))


def downgrade() -> None:
    # The switch is the owner's to turn off again in Control Center.
    pass
