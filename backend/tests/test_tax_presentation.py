"""Task 23 (audit-fixes-2026-08): tax presentation contracts.

Covers:
- dynamic ``year_hint`` carrying the computation year (Anlage KAP-INV hint),
- church-tax format unity: canonical internal form is a decimal fraction
  ("0.08"/"0.09"); integer-percent writes ("8"/"9") normalize on write and
  legacy stored values normalize on read.
"""

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.models.entities import AppSetting
from app.foundation.settings import get_public_settings, upsert_public_settings
from app.foundation.tax_cockpit import compute_year_overview


def _memory_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


# ---------------------------------------------------------------------------
# year_hint dynamism
# ---------------------------------------------------------------------------


def test_year_hint_contains_computation_year():
    db = _memory_db()
    overview = compute_year_overview(db, "user-1", 2031)
    mapping = overview["anlage_kap_mapping"]
    assert str(2031) in mapping["year_hint"]
    assert "Anlage KAP-INV" in mapping["year_hint"]


def test_year_hint_tracks_a_different_year():
    db = _memory_db()
    overview = compute_year_overview(db, "user-1", 2027)
    assert str(2027) in overview["anlage_kap_mapping"]["year_hint"]


# ---------------------------------------------------------------------------
# Church-tax normalization round-trips (canonical = decimal fraction)
# ---------------------------------------------------------------------------


def test_church_tax_integer_percent_normalized_on_write():
    db = _memory_db()
    result = upsert_public_settings(db, {"church_tax": "9"})
    assert result["church_tax"] == "0.09"
    assert get_public_settings(db)["church_tax"] == "0.09"


def test_church_tax_decimal_fraction_round_trips_unchanged():
    db = _memory_db()
    result = upsert_public_settings(db, {"church_tax": "0.08"})
    assert result["church_tax"] == "0.08"
    assert get_public_settings(db)["church_tax"] == "0.08"


def test_church_tax_integer_input_normalized_on_write():
    db = _memory_db()
    result = upsert_public_settings(db, {"church_tax": 8})
    assert result["church_tax"] == "0.08"


def test_church_tax_legacy_stored_percent_normalized_on_read():
    db = _memory_db()
    # Simulate a legacy row written before normalization existed.
    db.add(AppSetting(key="church_tax", value_json=json.dumps("9")))
    db.commit()
    assert get_public_settings(db)["church_tax"] == "0.09"


def test_church_tax_none_forms_stay_none():
    for raw in ("none", "", None, 0, "0"):
        fresh = _memory_db()
        result = upsert_public_settings(fresh, {"church_tax": raw})
        assert result["church_tax"] == "none", f"input {raw!r}"


def test_church_tax_malformed_value_degrades_to_none_without_crash():
    db = _memory_db()
    result = upsert_public_settings(db, {"church_tax": "abc"})
    assert result["church_tax"] == "none"


def test_church_tax_normalization_preserves_cockpit_rate():
    """End-to-end: legacy '9' must still yield the same computed 9% rate."""
    db = _memory_db()
    upsert_public_settings(db, {"church_tax": "9"})
    overview = compute_year_overview(db, "user-1", 2031)
    assert overview["tax"]["church_tax_rate"] == 0.09
