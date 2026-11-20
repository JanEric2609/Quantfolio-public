"""Read cache for ``get_public_settings`` / ``get_setting_row`` (audit D2, scalability).

The cache is process-local, keyed on the engine, TTL-bounded, and invalidated by
every write path, so these tests pin: fewer SELECTs, no stale reads after a
write, no cross-session leakage of uncommitted writes, no shared mutable state.
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation import settings as settings_mod
from app.foundation.core.db import Base
from app.foundation.models.entities import AppSetting
from app.foundation.settings import (
    SETTINGS_CACHE_TTL_SECONDS,
    delete_public_setting,
    get_public_settings,
    get_setting_row,
    invalidate_settings_cache,
    record_connection_test,
    resolve_frontend_origins,
    resolve_setting,
    settings_cache_generation,
    upsert_public_settings,
)


def _engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _session(engine, autoflush: bool = False):
    return sessionmaker(bind=engine, autoflush=autoflush, autocommit=False)()


class _SelectCounter:
    """Counts SELECTs against app_settings on an engine."""

    def __init__(self, engine):
        self.count = 0
        event.listen(engine, "before_cursor_execute", self._on_execute)

    def _on_execute(self, _conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT") and "FROM APP_SETTINGS" in statement.upper():
            self.count += 1


def test_second_read_does_not_touch_the_database():
    engine = _engine()
    db = _session(engine)
    counter = _SelectCounter(engine)

    first = get_public_settings(db)
    after_first = counter.count
    second = get_public_settings(db)
    get_setting_row(db, "currency")
    resolve_frontend_origins(db)

    assert after_first == 1
    assert counter.count == 1
    assert first == second


def test_many_reads_cost_one_select():
    engine = _engine()
    db = _session(engine)
    counter = _SelectCounter(engine)
    for _ in range(25):
        get_public_settings(db)
        resolve_setting(db, "llm_model", "LLM_MODEL", "x")
    assert counter.count == 1


def test_upsert_invalidates_so_the_next_read_sees_the_write():
    engine = _engine()
    db = _session(engine)
    assert get_public_settings(db)["currency"] == "EUR"
    generation = settings_cache_generation()

    returned = upsert_public_settings(db, {"currency": "USD"})

    assert returned["currency"] == "USD"
    assert get_public_settings(db)["currency"] == "USD"
    assert settings_cache_generation() > generation


def test_clearing_a_key_falls_back_to_the_default():
    engine = _engine()
    db = _session(engine)
    upsert_public_settings(db, {"currency": "USD"})
    assert get_public_settings(db)["currency"] == "USD"
    upsert_public_settings(db, {"currency": None})
    assert get_public_settings(db)["currency"] == "EUR"


def test_delete_public_setting_and_connection_test_invalidate():
    engine = _engine()
    db = _session(engine)
    upsert_public_settings(db, {"dkb_username": "someone"})
    assert get_public_settings(db)["dkb_username"] == "someone"
    delete_public_setting(db, "dkb_username")
    assert "dkb_username" not in get_public_settings(db)

    assert get_setting_row(db, "connection_tests_json") is None
    record_connection_test(db, "finnhub", True, "ok")
    assert get_setting_row(db, "connection_tests_json")["finnhub"]["ok"] is True


def test_direct_orm_write_by_another_session_is_seen_after_commit():
    """Code (and tests) that write AppSetting rows without the helpers still invalidate."""
    engine = _engine()
    reader = _session(engine)
    writer = _session(engine)
    assert get_public_settings(reader)["currency"] == "EUR"

    writer.add(AppSetting(key="currency", value_json=json.dumps("CHF")))
    writer.commit()
    assert get_public_settings(reader)["currency"] == "CHF"

    row = writer.query(AppSetting).filter(AppSetting.key == "currency").one()
    row.value_json = json.dumps("GBP")
    writer.commit()
    assert get_public_settings(reader)["currency"] == "GBP"

    writer.delete(row)
    writer.commit()
    assert get_public_settings(reader)["currency"] == "EUR"


def test_flushed_uncommitted_write_is_not_published_to_the_cache():
    engine = _engine()
    writer = _session(engine)
    assert get_public_settings(writer)["currency"] == "EUR"  # warms the cache

    writer.add(AppSetting(key="currency", value_json=json.dumps("USD")))
    writer.flush()
    # Flushed but uncommitted: read-your-writes inside the session...
    assert get_public_settings(writer)["currency"] == "USD"

    # The uncommitted value never reached the shared cache.
    with settings_mod._settings_cache_lock:
        cached = settings_mod._settings_cache.get(engine)
    assert cached is None or "currency" not in cached.rows

    writer.rollback()
    assert get_public_settings(writer)["currency"] == "EUR"
    assert writer.info.get(settings_mod._SETTINGS_DIRTY_KEY) is None


def test_pending_changes_are_visible_with_autoflush_sessions():
    engine = _engine()
    db = _session(engine, autoflush=True)
    assert get_public_settings(db)["currency"] == "EUR"
    db.add(AppSetting(key="currency", value_json=json.dumps("JPY")))
    assert get_public_settings(db)["currency"] == "JPY"
    db.commit()
    assert get_public_settings(db)["currency"] == "JPY"


def test_ttl_lets_out_of_band_writes_converge(monkeypatch):
    """A write made by another process (raw SQL here) shows up after the TTL."""
    engine = _engine()
    db = _session(engine)
    clock = [1000.0]
    monkeypatch.setattr(settings_mod, "_now", lambda: clock[0])

    assert get_public_settings(db)["currency"] == "EUR"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO app_settings (id, key, value_json, updated_at) "
                "VALUES ('raw-1', 'currency', :v, CURRENT_TIMESTAMP)"
            ),
            {"v": json.dumps("SEK")},
        )
    clock[0] += SETTINGS_CACHE_TTL_SECONDS - 0.1
    assert get_public_settings(db)["currency"] == "EUR"  # still inside the TTL
    clock[0] += 0.2
    assert get_public_settings(db)["currency"] == "SEK"  # expired: reloaded


def test_returned_values_cannot_mutate_the_cache_or_the_defaults():
    engine = _engine()
    db = _session(engine)
    upsert_public_settings(db, {"isin_ticker_overrides": {"DE000": "AAA"}, "tax_bafoeg": True})

    first = get_public_settings(db)
    first["isin_ticker_overrides"]["DE000"] = "MUTATED"
    first["currency"] = "MUTATED"
    first["etf_index_map"]["injected"] = "x"

    second = get_public_settings(db)
    assert second["isin_ticker_overrides"] == {"DE000": "AAA"}
    assert second["currency"] == "EUR"
    assert "injected" not in second["etf_index_map"]
    assert "injected" not in settings_mod.DEFAULT_PUBLIC_SETTINGS["etf_index_map"]


def test_engines_do_not_share_cache_entries():
    engine_a, engine_b = _engine(), _engine()
    db_a, db_b = _session(engine_a), _session(engine_b)
    upsert_public_settings(db_a, {"currency": "USD"})
    assert get_public_settings(db_a)["currency"] == "USD"
    assert get_public_settings(db_b)["currency"] == "EUR"


def test_invalidate_clears_a_warm_cache():
    engine = _engine()
    db = _session(engine)
    counter = _SelectCounter(engine)
    get_public_settings(db)
    invalidate_settings_cache()
    get_public_settings(db)
    assert counter.count == 2


def test_failed_load_is_not_cached():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )  # no tables yet
    db = _session(engine)
    try:
        get_public_settings(db)
    except Exception:
        db.rollback()
    else:  # pragma: no cover - the table is missing, this must raise
        raise AssertionError("expected an error for a missing table")
    Base.metadata.create_all(engine)
    assert get_public_settings(db)["currency"] == "EUR"
