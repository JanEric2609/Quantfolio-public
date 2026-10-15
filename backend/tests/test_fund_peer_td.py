"""C4 (plan 2026-10-14): fund choice reads peer tracking difference and domicile (ADR 0019, amended)."""
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision import fund_tracking
from app.decision.fund_tracking import annualised_return, peer_differences, refresh_peer_td, stored_peer_td
from app.decision.monthly_plan_funds import (
    CANDIDATES,
    W_DOMICILE,
    W_PLAN,
    W_REPLICATION,
    W_SIZE,
    W_TD,
    W_TER,
    W_VOLUME,
    FundCandidate,
    score_fund,
    suggest_fund,
    us_withholding_drag,
)
from app.foundation.core.db import Base

TODAY = date(2026, 10, 14)


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _rows(yearly_pct: float, years: int = 3, end: date = TODAY):
    start = end - timedelta(days=round(365.25 * years))
    out, d = [], start
    while d <= end:
        t = (d - start).days / 365.25
        out.append({"date": d, "close": 100.0 * (1 + yearly_pct / 100) ** t})
        d += timedelta(days=7)
    out[-1]["date"] = end
    return out


def test_weights_sum_to_one():
    assert W_TER + W_TD + W_SIZE + W_REPLICATION + W_VOLUME + W_PLAN + W_DOMICILE == pytest.approx(1.0)


def test_annualised_return_needs_the_whole_window():
    assert annualised_return(_rows(8.0), today=TODAY) == pytest.approx(8.0, abs=0.05)
    assert annualised_return(_rows(8.0, years=2), today=TODAY) is None
    assert annualised_return(_rows(8.0, end=TODAY - timedelta(days=30)), today=TODAY) is None
    assert annualised_return([], today=TODAY) is None


def test_peer_difference_only_within_an_index_with_two_funds():
    td = peer_differences({"A": 8.0, "B": 8.2, "C": 9.0}, {"A": "MSCI World", "B": "MSCI World", "C": "FTSE All-World"})
    assert td == {"A": pytest.approx(-0.1), "B": pytest.approx(0.1)}


def test_better_tracking_wins_and_unknown_is_neutral():
    a = FundCandidate("core", "IE00A", "A", "MSCI World", 0.20, 10.0, "full", 50.0, True, True)
    b = FundCandidate("core", "IE00B", "B", "MSCI World", 0.20, 10.0, "full", 50.0, True, True)
    td = {"IE00A": 0.05, "IE00B": -0.05}
    assert score_fund(a, td) > score_fund(a) > score_fund(b, td)
    assert score_fund(a, {"IE00A": 1.0}) - score_fund(a) == pytest.approx(W_TD * 0.5)


def test_irish_domicile_preferred_for_us_heavy_equity_only():
    lu_world = FundCandidate("core", "LU0000000001", "L", "MSCI World", 0.20, 10.0, "full", 50.0, True, True)
    ie_world = FundCandidate("core", "IE0000000001", "I", "MSCI World", 0.20, 10.0, "full", 50.0, True, True)
    lu_cash = next(c for c in CANDIDATES if c.sleeve == "cash")
    lu_europe = FundCandidate("universe", "LU0000000002", "E", "MSCI Europe", 0.20, 10.0, "full", 50.0, True, True)
    assert us_withholding_drag(lu_world) and not us_withholding_drag(ie_world)
    assert not us_withholding_drag(lu_cash) and not us_withholding_drag(lu_europe)
    assert score_fund(ie_world) - score_fund(lu_world) == pytest.approx(W_DOMICILE)


def test_curated_funds_with_peers_have_listings():
    by_index: dict[str, list[FundCandidate]] = {}
    for c in CANDIDATES:
        by_index.setdefault(c.index, []).append(c)
    shared = [c for group in by_index.values() if len(group) > 1 for c in group]
    assert shared and all(c.ticker for c in shared)


def test_refresh_stores_and_the_suggestion_cites_it(monkeypatch):
    db = _memory_db()
    yearly = {"EUNL.DE": 9.9, "XDWD.DE": 10.1, "IS3R.DE": 7.0, "XDEM.DE": 7.4, "VWCE.DE": 9.0}
    monkeypatch.setattr("app.foundation.market.history",
                        lambda _db, sym, days=0, allow_live=True: _rows(yearly[sym]) if sym in yearly else [])
    payload = refresh_peer_td(db, today=TODAY)
    assert payload["as_of"] == "2026-10-14"
    td = stored_peer_td(db)
    assert td["IE00BJ0KDQ92"] == pytest.approx(0.1, abs=0.01)
    assert td["IE00B4L5Y983"] == pytest.approx(-0.1, abs=0.01)
    assert "IE00BK5BQT80" not in td  # FTSE All-World has no curated peer
    core = suggest_fund("core", peer_td=td)
    assert core.isin == "IE00BJ0KDQ92"
    assert "+0.10 % a year against the median of its same-index peers" in core.why


def test_stored_peer_td_is_empty_before_the_first_run():
    assert stored_peer_td(_memory_db()) == {}
    assert fund_tracking.SETTING_KEY == "fund_peer_td_json"
