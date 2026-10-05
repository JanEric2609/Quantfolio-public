"""Control Center restructure: settings data clean-up.

* ``discover_investor_monthly_budget`` is dropped. It duplicated
  ``monthly_contribution_eur`` (prod held 150 next to 100); the plan's value
  is kept and the LLM portfolio context now reads it.
* ``webauthn_rp_name`` holding a URL is reset to the default display name —
  the field is a human-readable name shown in the passkey dialog.
* Alpaca's secret key was stored in plaintext ``api_keys.meta_json``. It is
  moved into the encrypted credential, packed as JSON {"key", "api_secret"}
  (the format ``settings.get_secret`` unpacks). Needs ENCRYPTION_KEY; without
  it the row is left alone, still readable, and repacked on its next save.

Downgrade restores nothing: the old budget value is a duplicate, and putting
a secret back into plaintext is not a state worth reproducing.

Revision ID: 0121_control_center_cleanup
Revises: 0120_index_country_weights
"""
import json
import logging

from alembic import op
import sqlalchemy as sa

revision = "0121_control_center_cleanup"
down_revision = "0120_index_country_weights"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("app_settings"):
        bind.execute(sa.text("DELETE FROM app_settings WHERE key = 'discover_investor_monthly_budget'"))
        row = bind.execute(
            sa.text("SELECT value_json FROM app_settings WHERE key = 'webauthn_rp_name'")
        ).fetchone()
        if row is not None:
            try:
                value = json.loads(row[0])
            except ValueError:
                value = None
            if not isinstance(value, str) or "://" in value:
                bind.execute(sa.text("DELETE FROM app_settings WHERE key = 'webauthn_rp_name'"))

    if inspector.has_table("api_keys"):
        _encrypt_alpaca_secret(bind)


def _encrypt_alpaca_secret(bind) -> None:
    row = bind.execute(
        sa.text("SELECT id, key_encrypted, meta_json FROM api_keys WHERE service = 'alpaca'")
    ).fetchone()
    if row is None:
        return
    meta = json.loads(row[2] or "{}")
    if "api_secret" not in meta:
        return
    try:
        from app.foundation.core.security import SecretBox

        box = SecretBox.from_settings()
        stored = box.decrypt(row[1])
    except Exception as exc:  # no ENCRYPTION_KEY here: leave it for the next save
        logger.warning("0121: Alpaca secret not re-encrypted (%s); it is repacked on its next save", exc)
        return
    try:
        packed = json.loads(stored)
    except ValueError:
        packed = None
    if not isinstance(packed, dict) or "key" not in packed:
        packed = {"key": stored}
    packed.setdefault("api_secret", meta.pop("api_secret"))
    meta.pop("api_secret", None)
    bind.execute(
        sa.text("UPDATE api_keys SET key_encrypted = :enc, meta_json = :meta WHERE id = :id"),
        {"enc": box.encrypt(json.dumps(packed)), "meta": json.dumps(meta), "id": row[0]},
    )


def downgrade() -> None:
    pass
