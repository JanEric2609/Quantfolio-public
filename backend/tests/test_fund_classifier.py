"""Unified evidence-based fund classifier (plan todo 22 / Metis B2).

THE RULE: classification comes ONLY from verifiable positive evidence —
(1) ``holdings.fund_class`` explicitly set by the user, or
(2) an etf_universe ISIN/composition match (keyword heuristics apply ONLY
    inside that match path, demoted to a hint).
No evidence → ``"other"`` (0 % Teilfreistellung): the conservative floor can
only OVERSTATE estimated tax, never understate it.
"""
from datetime import date
from datetime import datetime
from datetime import timezone
from decimal import Decimal

import pytest
from conftest import _memory_db

from app.foundation.models.entities import Holding, Portfolio, PriceCache, User
from app.foundation import tax_cockpit
from app.foundation.fund_class import (
    classify_fund_class,
    classify_fund_class_with_source,
)


def _user(db):
    user = User(username="jan", password_hash="hash")
    db.add(user)
    db.commit()
    return user


# ---------------------------------------------------------------------------
# Pure classifier matrix
# ---------------------------------------------------------------------------


def test_user_set_class_wins_over_etf_match():
    """Evidence priority 1: an explicit user fund_class beats any universe match."""
    holding = {
        "fund_class": "misch",
        "isin": "IE00B4L5Y983",
        "etf_universe_match": {"isin": "IE00B4L5Y983", "name": "iShares Core MSCI World UCITS ETF"},
    }
    assert classify_fund_class(holding) == "misch"
    assert classify_fund_class_with_source(holding) == ("misch", "user")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("iShares Core MSCI World UCITS ETF", "aktien"),
        ("Vanguard FTSE All-World UCITS ETF", "aktien"),
        ("iShares Core Euro Government Bond UCITS ETF", "other"),
        ("Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C", "other"),
    ],
)
def test_etf_universe_match_is_second_evidence(name, expected):
    """Evidence priority 2: a matched universe record classifies via the hint."""
    holding = {"fund_class": None, "isin": "XX", "etf_universe_match": {"name": name}}
    assert classify_fund_class_with_source(holding) == (expected, "etf_universe")


def test_no_evidence_defaults_to_other_floor():
    holding = {"fund_class": None, "isin": "LU0000000001", "name": "Some Fund"}
    assert classify_fund_class(holding) == "other"
    assert classify_fund_class_with_source(holding) == ("other", "default_other")


def test_empty_holding_defaults_to_other_floor():
    assert classify_fund_class({}) == "other"
    assert classify_fund_class_with_source({}) == ("other", "default_other")


def test_invalid_user_value_is_not_evidence():
    """A garbage fund_class string is not verifiable evidence → floor."""
    holding = {"fund_class": "equities-ish", "isin": "IE00B4L5Y983"}
    assert classify_fund_class_with_source(holding) == ("other", "default_other")


def test_explicit_user_other_is_user_sourced():
    """The user may explicitly pin 'other' — that is evidence, not the floor."""
    holding = {"fund_class": "other", "isin": "IE00B4L5Y983"}
    assert classify_fund_class_with_source(holding) == ("other", "user")


def test_all_valid_classes_pass_through_as_user_evidence():
    for value in ("aktien", "misch", "immobilien"):
        assert classify_fund_class_with_source({"fund_class": value}) == (value, "user")


# ---------------------------------------------------------------------------
# Cockpit integration: holdings Vorabpauschale estimation
# ---------------------------------------------------------------------------


def _seed_fund_holding(db, user, *, isin="IE00B4L5Y983", ticker="IWDA", name="iShares Core MSCI World"):
    pf = Portfolio(user_id=user.id, name="Main")
    db.add(pf)
    db.commit()
    db.add(
        Holding(
            portfolio_id=pf.id,
            isin=isin,
            ticker=ticker,
            name=name,
            asset_type="etf",
            quantity=Decimal("100"),
            avg_buy_price=Decimal("90"),
        )
    )
    year = 2024
    now = datetime.now(timezone.utc)
    db.add(PriceCache(ticker=ticker, date=date(year, 1, 2), close=Decimal("100"), fetched_at=now))
    db.add(PriceCache(ticker=ticker, date=date(year, 1, 3), close=Decimal("110"), fetched_at=now))
    db.commit()
    return year


def test_cockpit_etf_matched_equity_gets_aktien_teilfreistellung(monkeypatch):
    """A holding matched in etf_universe as an equity fund applies 30 % TF."""
    db = _memory_db()
    user = _user(db)
    year = _seed_fund_holding(db, user)

    monkeypatch.setattr(
        tax_cockpit,
        "_load_etf_universe_index",
        lambda: {"IE00B4L5Y983": {"isin": "IE00B4L5Y983", "name": "iShares Core MSCI World UCITS ETF"}},
    )

    items, total = tax_cockpit._estimate_holdings_vorabpauschale(db, user.id, year)

    assert len(items) == 1
    assert items[0]["fund_class"] == "aktien"
    assert items[0]["classification"] == {"value": "aktien", "source": "etf_universe"}
    assert items[0]["teilfreistellung_pct"] == 0.30
    vorab = Decimal(str(items[0]["vorabpauschale_eur"]))
    assert vorab > 0
    # 30 % Teilfreistellung applied: taxable == 0.70 * raw Vorabpauschale.
    assert abs(total - vorab * Decimal("0.70")) < Decimal("0.01")


def test_cockpit_no_evidence_stays_other_zero_tf(monkeypatch):
    """Without any evidence the conservative floor holds: other / 0 % TF."""
    db = _memory_db()
    user = _user(db)
    year = _seed_fund_holding(db, user)

    monkeypatch.setattr(tax_cockpit, "_load_etf_universe_index", lambda: {})

    items, total = tax_cockpit._estimate_holdings_vorabpauschale(db, user.id, year)

    assert len(items) == 1
    assert items[0]["fund_class"] == "other"
    assert items[0]["classification"] == {"value": "other", "source": "default_other"}
    assert items[0]["teilfreistellung_pct"] == 0.0
    vorab = Decimal(str(items[0]["vorabpauschale_eur"]))
    assert vorab > 0
    assert abs(total - vorab) < Decimal("0.01")  # no Teilfreistellung applied


def test_cockpit_user_lot_beats_etf_universe(monkeypatch):
    """User-set TaxLot classification wins even when the universe would say aktien."""
    db = _memory_db()
    user = _user(db)
    year = _seed_fund_holding(db, user, isin="LU0000000001", ticker="MISCH", name="Mixed Allocation Fund")
    tax_cockpit.create_lot(
        db,
        user.id,
        {
            "isin": "LU0000000001",
            "acquired_at": date(2023, 1, 10),
            "quantity": 100,
            "cost_basis_eur": 9000,
            "fund_class": "misch",
        },
    )

    monkeypatch.setattr(
        tax_cockpit,
        "_load_etf_universe_index",
        lambda: {"LU0000000001": {"isin": "LU0000000001", "name": "Some Equity UCITS ETF"}},
    )

    items, total = tax_cockpit._estimate_holdings_vorabpauschale(db, user.id, year)

    assert len(items) == 1
    assert items[0]["classification"] == {"value": "misch", "source": "user"}
    assert items[0]["teilfreistellung_pct"] == 0.15
    vorab = Decimal(str(items[0]["vorabpauschale_eur"]))
    assert abs(total - vorab * Decimal("0.85")) < Decimal("0.01")


def test_cockpit_etf_lookup_throw_caught_other_with_warning(monkeypatch, caplog):
    """A throwing etf_universe lookup degrades to the floor with a warning."""
    db = _memory_db()
    user = _user(db)
    year = _seed_fund_holding(db, user)

    def _boom():
        raise RuntimeError("justETF exploded")

    monkeypatch.setattr(tax_cockpit, "_load_etf_universe_index", _boom)

    with caplog.at_level("WARNING"):
        items, total = tax_cockpit._estimate_holdings_vorabpauschale(db, user.id, year)

    assert len(items) == 1
    assert items[0]["classification"] == {"value": "other", "source": "default_other"}
    assert items[0]["teilfreistellung_pct"] == 0.0
    assert any("etf_universe" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Single-holding estimate endpoint service
# ---------------------------------------------------------------------------


def test_estimate_vorabpauschale_records_classification_provenance(monkeypatch):
    monkeypatch.setattr(
        tax_cockpit,
        "_load_etf_universe_index",
        lambda: {"IE00B4L5Y983": {"isin": "IE00B4L5Y983", "name": "iShares Core MSCI World UCITS ETF"}},
    )
    out = tax_cockpit.estimate_vorabpauschale(
        isin="IE00B4L5Y983",
        fund_value_start_of_year_eur=Decimal("10000"),
        fund_value_end_of_year_eur=Decimal("11000"),
        distributions_during_year_eur=Decimal("0"),
        tax_year=2024,
    )
    assert out["classification"] == {"value": "aktien", "source": "etf_universe"}
    assert out["teilfreistellung_pct"] == 0.30
    assert out["estimate"] is True and out["not_tax_advice"] is True


def test_estimate_vorabpauschale_explicit_class_is_user_sourced(monkeypatch):
    monkeypatch.setattr(tax_cockpit, "_load_etf_universe_index", lambda: {})
    out = tax_cockpit.estimate_vorabpauschale(
        isin="LU0000000001",
        fund_value_start_of_year_eur=Decimal("10000"),
        fund_value_end_of_year_eur=Decimal("11000"),
        distributions_during_year_eur=Decimal("0"),
        tax_year=2024,
        fund_class="misch",
    )
    assert out["classification"] == {"value": "misch", "source": "user"}
    assert out["teilfreistellung_pct"] == 0.15


# ---------------------------------------------------------------------------
# etf_universe shim compatibility
# ---------------------------------------------------------------------------


def test_derive_tf_class_shim_delegates_to_util_hint():
    from app.foundation.etf_universe import _derive_tf_class

    assert _derive_tf_class("Vanguard FTSE All-World UCITS ETF", None) == "aktien"
    assert _derive_tf_class("iShares Core Euro Government Bond UCITS ETF", None) == "other"
    assert _derive_tf_class("Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C", None) == "other"


def test_provider_get_universe_cached_never_live_fetches(monkeypatch):
    """get_universe_cached must not fall back to a live justETF fetch."""
    from app.foundation.etf_universe import EtfUniverseProvider

    provider = EtfUniverseProvider(cache_ttl_hours=0)  # force cache stale/absent

    def _no_fetch():
        raise AssertionError("live fetch attempted from get_universe_cached")

    monkeypatch.setattr(EtfUniverseProvider, "_fetch_from_justetf", staticmethod(_no_fetch))
    assert provider.get_universe_cached() == []
