"""The ETF lane is plan driven and index de-duplicated (plan 2026-10-12, D4 / WP4b-c)."""
from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from conftest import _memory_db

from app.decision.discover import universe as universe_mod
from app.decision.discover.universe import build_universe
from app.decision.etf_suggestions import asset_class_suggestions, best_per_index
from app.foundation.models.entities import Holding, Portfolio, User
from app.foundation.settings import upsert_public_settings


def _etf(symbol, isin, name, ter=0.2, size=1000, replication="Full replication"):
    return {"symbol": symbol, "isin": isin, "name": name, "ter": ter, "size_eur_m": size,
            "replication": replication, "strategy": "Long-only", "tf_class": "aktien",
            "domicile_country": "IE", "dividends": "Accumulating"}


UNIVERSE = [
    _etf("VWCE.DE", "IE00BK5BQT80", "Vanguard FTSE All-World UCITS ETF (USD) Accumulating", 0.14, 55_367,
         "Optimized sampling"),
    _etf("VGWL.DE", "IE00B3RBWM25", "Vanguard FTSE All-World UCITS ETF (USD) Distributing", 0.19, 16_000,
         "Optimized sampling"),
    _etf("IS3N.DE", "IE00BKM4GZ66", "iShares Core MSCI Emerging Markets IMI UCITS ETF (Acc)", 0.18, 40_259),
    _etf("XMME.DE", "IE00BTJRMP35", "Xtrackers MSCI Emerging Markets UCITS ETF 1C", 0.18, 8_000),
    _etf("IS3S.DE", "IE00BP3QZB59", "iShares Edge MSCI World Value Factor UCITS ETF", 0.25, 6_730),
    _etf("XDEV.DE", "IE00BL25JM42", "Xtrackers MSCI World Value UCITS ETF 1C", 0.25, 3_000),
    _etf("IUSZ.DE", "IE0005042456", "iShares Core FTSE 100 UCITS ETF GBP (Dist)", 0.07, 15_000),
    _etf("QQQ3.DE", "IE00BLRPRL42", "WisdomTree NASDAQ 100 3x Daily Leveraged", 0.75, 400),
    _etf("SPYX.DE", "IE00B3YCGJ38", "Invesco S&P 500 UCITS ETF", 0.05, 30_000, "Swap based Unfunded"),
]


class _Provider:
    def get_universe(self):
        return [dict(e) for e in UNIVERSE]


def _user_with_book(db) -> User:
    user = User(id=uuid4().hex, username="u", password_hash="x")
    db.add(user)
    db.flush()
    pf = Portfolio(id=uuid4().hex, user_id=user.id, name="Main")
    db.add(pf)
    db.flush()
    # The owner holds VWRA (same ISIN as VWCE) and EIMI (an EM IMI fund on another listing).
    db.add_all([
        Holding(id=uuid4().hex, portfolio_id=pf.id, ticker="VWRA.L", isin="IE00BK5BQT80",
                name="Vanguard FTSE All-World UCITS ETF", quantity=10, avg_buy_price=100),
        Holding(id=uuid4().hex, portfolio_id=pf.id, ticker="EIMI.L", isin="IE00BKM4GZ66",
                name="iShares Core MSCI EM IMI UCITS ETF", quantity=10, avg_buy_price=30),
    ])
    db.commit()
    return user


def _etfs(db, user) -> dict[str, dict]:
    with (
        patch.object(universe_mod, "_get_etf_provider", return_value=_Provider()),
        patch.object(universe_mod, "_fetch_live_fundamentals_no_db", side_effect=lambda t, registry: (t, {})),
    ):
        candidates = build_universe(db, user.id)
    return {c["symbol"]: c for c in candidates if c["source"] == "screen_etf"}


def test_same_isin_as_a_holding_is_never_suggested():
    db = _memory_db()
    etfs = _etfs(db, _user_with_book(db))
    assert "VWCE.DE" not in etfs


def test_a_held_index_is_never_suggested_under_another_isin():
    db = _memory_db()
    etfs = _etfs(db, _user_with_book(db))
    assert "VGWL.DE" not in etfs  # FTSE All-World, held as VWRA
    assert "IS3N.DE" not in etfs  # EM IMI, held as EIMI


def test_one_fund_per_index():
    db = _memory_db()
    etfs = _etfs(db, _user_with_book(db))
    value = [c for c in etfs.values() if c["index_key"] == "MSCI World Value"]
    assert [c["symbol"] for c in value] == ["IS3S.DE"]  # the larger of two equal-TER funds
    assert value[0]["plan_line"] == "tilt"


def test_index_outside_the_plan_leveraged_and_stray_swaps_are_not_candidates():
    db = _memory_db()
    etfs = _etfs(db, _user_with_book(db))
    assert "IUSZ.DE" not in etfs
    assert "QQQ3.DE" not in etfs
    assert "SPYX.DE" not in etfs


def test_best_per_index_prefers_lower_cost():
    best = best_per_index([
        _etf("A", "IE00BK5BQT80", "Vanguard FTSE All-World UCITS ETF (USD) Accumulating", 0.14, 55_000),
        _etf("B", "IE00B3RBWM25", "Vanguard FTSE All-World UCITS ETF (USD) Distributing", 0.30, 55_000),
    ])
    assert best["FTSE All-World"]["symbol"] == "A"


def test_plan_gap_lines_info_list_and_coverage():
    db = _memory_db()
    upsert_public_settings(db, {"plan_bond_target_pct": 10, "plan_gold_target_pct": 3})
    from app.foundation.settings import get_public_settings

    positions = [
        {"isin": "IE00BK5BQT80", "ticker": "VWRA.L", "name": "Vanguard FTSE All-World UCITS ETF",
         "value_eur": 9_000.0},
    ]
    out = asset_class_suggestions(get_public_settings(db), positions, book_eur=10_000.0, universe=UNIVERSE)

    assert out["estimate"] is True and out["not_tax_advice"] is True
    lines = {line["line"]: line for line in out["lines"]}
    assert set(lines) == {"bond", "gold"}
    bond = lines["bond"]
    assert bond["status"] == "under" and bond["gap_pp"] == pytest.approx(10.0)
    assert bond["fund"]["isin"] == "IE00B3VTMJ91"
    assert bond["amount_eur"] == pytest.approx(1_000.0)
    assert "Teilfreistellung 0 %" in bond["tax_note"]
    gold = lines["gold"]
    assert gold["fund"]["isin"] == "DE000A0S9GB0"
    assert "§ 23 EStG" in gold["tax_note"] and "12 months" in gold["tax_note"]
    assert "not a forecast" in gold["rule_note"]
    info = {i["index"] for i in out["info"]}
    assert "FTSE 100" in info and "S&P 500" not in info  # the swap clone is dropped, not listed
    assert {"index": "FTSE All-World", "held_by": "VWRA.L"} in out["covered"]


def test_no_targets_no_lines():
    out = asset_class_suggestions({}, [], book_eur=10_000.0, universe=[])
    assert out["lines"] == []
