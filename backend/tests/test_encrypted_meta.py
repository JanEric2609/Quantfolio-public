"""DKB username and Telegram webhook secret live inside the encrypted credential.

They used to sit in plaintext ``api_keys.meta_json``. They now use the same
packing as Alpaca's secret key, and an existing plaintext copy is folded into the
encrypted value the first time the row is read — never lost on the way.
"""
import json

from conftest import _memory_db

from app.foundation.core.security import SecretBox
from app.foundation.models.entities import ApiKey
from app.foundation.settings import get_secret, set_secret, update_secret_meta
from app.foundation.settings_catalog import sensitive_meta_keys
from app.foundation.telegram_bot import ensure_webhook_secret, verify_webhook_secret


def _row(db, service):
    return db.query(ApiKey).filter(ApiKey.service == service).one()


def _decrypted(db, service):
    return SecretBox.from_settings().decrypt(_row(db, service).key_encrypted)


def test_catalog_marks_both_keys_as_encrypted_meta():
    assert "username" in sensitive_meta_keys("dkb")
    assert "webhook_secret" in sensitive_meta_keys("telegram")
    assert "api_secret" in sensitive_meta_keys("alpaca")
    assert sensitive_meta_keys("finnhub") == set()


def test_dkb_username_is_encrypted_and_still_readable():
    db = _memory_db()

    set_secret(db, "dkb", "123456", {"username": "jan.login", "product_id": "PID"})

    row = _row(db, "dkb")
    assert "jan.login" not in row.meta_json
    assert json.loads(row.meta_json) == {"product_id": "PID"}
    assert "jan.login" in _decrypted(db, "dkb")
    assert get_secret(db, "dkb") == ("123456", {"username": "jan.login", "product_id": "PID"})


def test_dkb_pin_that_looks_like_json_survives_packing():
    db = _memory_db()

    set_secret(db, "dkb", "424242", {"username": "jan"})

    assert get_secret(db, "dkb")[0] == "424242"


def test_username_only_update_keeps_the_pin_and_stays_encrypted():
    db = _memory_db()
    set_secret(db, "dkb", "123456", {"username": "old"})

    update_secret_meta(db, "dkb", {"username": "new"})

    assert "new" not in _row(db, "dkb").meta_json
    assert get_secret(db, "dkb") == ("123456", {"username": "new"})


def test_blank_username_leaves_it_unchanged_and_none_clears_it():
    db = _memory_db()
    set_secret(db, "dkb", "123456", {"username": "jan"})

    set_secret(db, "dkb", "654321", {"username": ""})
    assert get_secret(db, "dkb") == ("654321", {"username": "jan"})

    update_secret_meta(db, "dkb", {"username": None})
    assert get_secret(db, "dkb") == ("654321", {})


def test_legacy_plaintext_dkb_username_is_moved_on_first_read_without_loss():
    db = _memory_db()
    box = SecretBox.from_settings()
    db.add(
        ApiKey(
            service="dkb",
            key_encrypted=box.encrypt("123456"),  # a bare PIN, as written before packing existed
            meta_json=json.dumps({"username": "jan.login", "product_id": "PID", "tan_medium": "app"}),
        )
    )
    db.commit()

    first = get_secret(db, "dkb")

    assert first == ("123456", {"username": "jan.login", "product_id": "PID", "tan_medium": "app"})
    row = _row(db, "dkb")
    assert "jan.login" not in row.meta_json
    assert json.loads(row.meta_json) == {"product_id": "PID", "tan_medium": "app"}
    assert json.loads(box.decrypt(row.key_encrypted)) == {"key": "123456", "username": "jan.login"}
    # Reading again is a no-op that returns the same thing.
    assert get_secret(db, "dkb") == first


def test_encrypted_value_wins_over_a_stale_plaintext_copy():
    db = _memory_db()
    box = SecretBox.from_settings()
    db.add(
        ApiKey(
            service="dkb",
            key_encrypted=box.encrypt(json.dumps({"key": "123456", "username": "current"})),
            meta_json=json.dumps({"username": "stale"}),
        )
    )
    db.commit()

    assert get_secret(db, "dkb") == ("123456", {"username": "current"})
    assert "stale" not in _row(db, "dkb").meta_json


def test_undecryptable_row_is_not_rewritten():
    db = _memory_db()
    db.add(ApiKey(service="dkb", key_encrypted="not-a-fernet-token", meta_json=json.dumps({"username": "jan"})))
    db.commit()

    try:
        get_secret(db, "dkb")
    except ValueError:
        pass

    assert json.loads(_row(db, "dkb").meta_json) == {"username": "jan"}


def test_telegram_webhook_secret_is_encrypted_and_verifies():
    db = _memory_db()
    set_secret(db, "telegram", "123:ABC")

    secret = ensure_webhook_secret(db)

    assert secret not in _row(db, "telegram").meta_json
    assert secret in _decrypted(db, "telegram")
    assert ensure_webhook_secret(db) == secret
    assert get_secret(db, "telegram") == ("123:ABC", {"webhook_secret": secret})
    assert verify_webhook_secret(db, secret) is True
    assert verify_webhook_secret(db, "wrong") is False


def test_legacy_plaintext_webhook_secret_keeps_working_and_is_moved():
    db = _memory_db()
    box = SecretBox.from_settings()
    db.add(
        ApiKey(
            service="telegram",
            key_encrypted=box.encrypt("123:ABC"),
            meta_json=json.dumps({"webhook_secret": "legacy-secret"}),
        )
    )
    db.commit()

    assert verify_webhook_secret(db, "legacy-secret") is True

    assert "legacy-secret" not in _row(db, "telegram").meta_json
    assert ensure_webhook_secret(db) == "legacy-secret"
    assert verify_webhook_secret(db, "legacy-secret") is True


def test_resaving_the_bot_token_keeps_the_webhook_secret():
    db = _memory_db()
    set_secret(db, "telegram", "123:ABC")
    secret = ensure_webhook_secret(db)

    set_secret(db, "telegram", "999:NEW")

    assert get_secret(db, "telegram") == ("999:NEW", {"webhook_secret": secret})
