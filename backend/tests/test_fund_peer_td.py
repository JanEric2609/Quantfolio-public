"""C4 (plan 2026-10-14): fund choice reads peer tracking difference and domicile (ADR 0019, amended)."""
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision import fund_tracking
from app.decision.fund_tracking import (
    aligned_returns,
    annualised_return,
    peer_differences,
    refresh_peer_td,
    stored_peer_td,
)
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
    td = stored_peer_td(db, today=TODAY)
    assert td["IE00BJ0KDQ92"] == pytest.approx(0.1, abs=0.01)
    assert td["IE00B4L5Y983"] == pytest.approx(-0.1, abs=0.01)
    assert "IE00BK5BQT80" not in td  # FTSE All-World has no curated peer
    core = suggest_fund("core", peer_td=td)
    assert core.isin == "IE00BJ0KDQ92"
    assert "+0.10 % a year against the median of its same-index peers" in core.why


def test_stored_peer_td_is_empty_before_the_first_run():
    assert stored_peer_td(_memory_db()) == {}
    assert fund_tracking.SETTING_KEY == "fund_peer_td_json"


def test_peers_are_measured_between_the_same_two_dates():
    # Identical funds; B's feed is three days behind, after a 5 % jump that only A has priced.
    a = _rows(8.0)
    a[-1]["close"] *= 1.05
    b = _rows(8.0, end=TODAY - timedelta(days=3))
    pts = {"A": fund_tracking._points(a), "B": fund_tracking._points(b)}
    index = {"A": "MSCI World", "B": "MSCI World"}
    naive = {"A": annualised_return(a, today=TODAY), "B": annualised_return(b, today=TODAY)}
    assert abs(naive["A"] - naive["B"]) > 1.0
    aligned = aligned_returns(pts, index, today=TODAY)
    assert aligned["A"] == pytest.approx(aligned["B"], abs=0.05)


def test_an_outage_keeps_the_last_good_figures_and_old_ones_age_out(monkeypatch):
    db = _memory_db()
    yearly = {"EUNL.DE": 9.9, "XDWD.DE": 10.1}
    monkeypatch.setattr("app.foundation.market.history",
                        lambda _db, sym, days=0, allow_live=True: _rows(yearly[sym]) if sym in yearly else [])
    refresh_peer_td(db, today=TODAY)
    good = stored_peer_td(db, today=TODAY)
    assert good

    def down(*a, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.foundation.market.history", down)
    assert refresh_peer_td(db, today=TODAY + timedelta(days=30))["td_pct"] == {}
    assert stored_peer_td(db, today=TODAY + timedelta(days=30)) == good
    assert stored_peer_td(db, today=TODAY + timedelta(days=fund_tracking.MAX_AGE_DAYS + 1)) == {}


def test_tracking_difference_never_picks_between_indexes():
    tilt = [c for c in CANDIDATES if c.sleeve == "tilt"]
    assert len({c.index for c in tilt}) > 1
    plain = suggest_fund("tilt")
    for c in tilt:
        favoured = suggest_fund("tilt", peer_td={c.isin: 5.0})
        index = next(x.index for x in CANDIDATES if x.isin == favoured.isin)
        assert index == next(x.index for x in CANDIDATES if x.isin == plain.isin)


# --- the Trust page's "funds" row --------------------------------------------------


def _hold(db, user_id, isin, qty=10, price=100.0):
    from decimal import Decimal

    from app.foundation.models.entities import BrokerPosition, ConnectedAccount

    account = db.query(ConnectedAccount).filter(ConnectedAccount.user_id == user_id).first()
    if account is None:
        account = ConnectedAccount(user_id=user_id, source="scalable", name="Depot", account_type="depot")
        db.add(account)
        db.commit()
    db.add(BrokerPosition(user_id=user_id, connected_account_id=account.id, source="scalable", isin=isin, name=isin,
                          quantity=Decimal(qty), current_price=Decimal(str(price)),
                          current_value=Decimal(str(qty * price))))
    db.commit()


def test_held_funds_compare_each_curated_fund_with_its_best_peer():
    from app.foundation.models.entities import User
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    user = User(username="f", password_hash="x")
    db.add(user)
    db.commit()
    assert fund_tracking.held_funds(db, user.id)["funds"] == []

    _hold(db, user.id, "IE00B4L5Y983", price=110.0)   # iShares MSCI World, trails its peer by 0.2 % a year
    _hold(db, user.id, "IE00BK5BQT80")                # FTSE All-World: no measured peer
    _hold(db, user.id, "US0378331005")                # a stock: not compared
    upsert_public_settings(db, {fund_tracking.SETTING_KEY: {
        "as_of": "2026-10-01", "years": 3, "td_pct": {"IE00B4L5Y983": -0.1, "IE00BJ0KDQ92": 0.1},
    }})

    out = fund_tracking.held_funds(db, user.id, today=date(2026, 10, 15))
    assert out["as_of"] == "2026-10-01" and out["n_other_positions"] == 1
    world, all_world = out["funds"]
    assert world["isin"] == "IE00B4L5Y983" and world["td_pct"] == -0.1
    assert world["best_peer"]["isin"] == "IE00BJ0KDQ92" and world["gap_pct"] == pytest.approx(0.2)
    assert world["flag"] is True
    assert all_world["td_pct"] is None and all_world["best_peer"] is None and all_world["flag"] is False
