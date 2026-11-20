"""The advisor proposal stays on the owner's plan (plan 2026-10-12, D5 / WP2)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pytest
from conftest import _memory_db

from app.foundation.models.entities import ListingCurrency, PaperHolding, PaperPortfolio, PriceCache, User
from app.foundation.quant_proposal import PROPOSAL_MAX_TURNOVER, build_trade_proposal

pytest.importorskip("skfolio")

PLAN = {
    "core_min": 0.85,  # 90 % core target minus a 5 pp band
    "satellite_max": 0.10,
    "name_max": 0.05,
    "core_tickers": ["EUNL.DE"],
    "core_isins": ["IE00B4L5Y983"],
}


def _seed(db, ticker: str, closes: np.ndarray) -> None:
    db.add(ListingCurrency(symbol=ticker, currency="EUR", source="test"))
    days: list[date] = []
    d = date.today()
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    now = datetime.now(UTC)
    for dd, c in zip(reversed(days), closes):
        db.add(PriceCache(id=uuid4().hex, ticker=ticker, date=dd, close=Decimal(str(round(float(c), 4))),
                          fetched_at=now, source="test", stale=False, currency="EUR"))


def _book(db) -> str:
    """94 % EUNL plus a little DELL, and five candidates that went straight up."""
    rng = np.random.default_rng(7)
    n = 480
    market = rng.normal(0.0003, 0.010, n)
    _seed(db, "EUNL.DE", 80 * np.cumprod(1 + market))
    _seed(db, "DELL", 100 * np.cumprod(1 + 1.3 * market + rng.normal(0.0015, 0.02, n)))
    for i, sym in enumerate(["CBE3X", "IUSZX", "EUNKX", "KMIX", "HOTX"]):
        # High sample means: what max_ratio chased.
        _seed(db, sym, 50 * np.cumprod(1 + 0.8 * market + rng.normal(0.002 + 0.0005 * i, 0.015, n)))
    user = User(id=uuid4().hex, username="u", password_hash="x")
    db.add(user)
    db.flush()
    pf = PaperPortfolio(id=uuid4().hex, user_id=user.id, name="advisor")
    db.add(pf)
    db.flush()
    eunl_price = float(db.query(PriceCache).filter(PriceCache.ticker == "EUNL.DE").order_by(PriceCache.date.desc()).first().close)
    dell_price = float(db.query(PriceCache).filter(PriceCache.ticker == "DELL").order_by(PriceCache.date.desc()).first().close)
    db.add_all([
        PaperHolding(portfolio_id=pf.id, isin="IE00B4L5Y983", ticker="EUNL.DE", name="iShares Core MSCI World",
                     asset_type="etf", quantity=Decimal(str(94_000 / eunl_price)), avg_buy_price=Decimal("80")),
        PaperHolding(portfolio_id=pf.id, ticker="DELL", name="Dell", asset_type="stock",
                     quantity=Decimal(str(6_000 / dell_price)), avg_buy_price=Decimal("100")),
    ])
    db.commit()
    return pf.id


CANDIDATES = [{"symbol": s, "source": "screen_index"} for s in ["CBE3X", "IUSZX", "EUNKX", "KMIX", "HOTX"]]


def test_core_stays_at_least_target_minus_band():
    db = _memory_db()
    pf_id = _book(db)
    proposal = build_trade_proposal(db, pf_id, CANDIDATES, config={"plan_targets": PLAN})
    assert proposal.optimizer_status == "completed", proposal.notes
    w = proposal.suggested_weights
    assert w["EUNL.DE"] >= 0.85 - 1e-4
    satellite = sum(v for k, v in w.items() if k != "EUNL.DE")
    assert satellite <= 0.10 + 1e-4
    assert all(v <= 0.05 + 1e-4 for k, v in w.items() if k != "EUNL.DE")


def test_turnover_cap_is_honoured():
    db = _memory_db()
    pf_id = _book(db)
    proposal = build_trade_proposal(db, pf_id, CANDIDATES, config={"plan_targets": PLAN})
    total = sum(proposal.current_book.values())
    for sym, target in proposal.suggested_weights.items():
        current = proposal.current_book.get(sym, 0.0) / total
        assert abs(target - current) <= PROPOSAL_MAX_TURNOVER + 1e-4


def test_other_core_funds_are_not_bought_in_place_of_the_core_fund():
    # Replay of 2026-10-06..09: minimum variance sold EUNL into VWRA/EIMI, both core ISINs.
    db = _memory_db()
    pf_id = _book(db)
    rng = np.random.default_rng(11)
    eunl = [float(r.close) for r in db.query(PriceCache).filter(PriceCache.ticker == "EUNL.DE").order_by(PriceCache.date)]
    rets = np.diff(eunl) / np.array(eunl[:-1])
    # A core-ISIN fund with slightly lower variance than EUNL (what min variance prefers).
    _seed(db, "VWRAX", 100 * np.cumprod(np.r_[1.0, 1 + 0.85 * rets + rng.normal(0, 0.001, len(rets))]))
    db.commit()
    plan = {**PLAN, "core_isins": ["IE00B4L5Y983", "IE00BK5BQT80"]}
    cands = CANDIDATES + [{"symbol": "VWRAX", "isin": "IE00BK5BQT80", "source": "screen_etf"}]
    proposal = build_trade_proposal(db, pf_id, cands, config={"plan_targets": plan})
    assert proposal.optimizer_status == "completed", proposal.notes
    assert proposal.suggested_weights.get("VWRAX", 0.0) <= 1e-4
    assert proposal.suggested_weights["EUNL.DE"] >= 0.85 - 1e-4


def test_core_is_added_to_the_universe_when_not_held():
    db = _memory_db()
    pf_id = _book(db)
    db.query(PaperHolding).filter(PaperHolding.ticker == "EUNL.DE").delete()
    db.commit()
    proposal = build_trade_proposal(db, pf_id, CANDIDATES, config={"plan_targets": PLAN})
    assert "EUNL.DE" in proposal.suggested_weights
    # A book with no core can only move 20 pp toward it in one step.
    assert proposal.suggested_weights["EUNL.DE"] == pytest.approx(PROPOSAL_MAX_TURNOVER, abs=1e-3)
    assert any("core floor" in n for n in proposal.notes)
