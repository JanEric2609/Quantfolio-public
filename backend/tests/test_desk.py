"""The guarded derivatives desk (ADR 0027): limits, guard, sizing, ledger, products, signals, scoring."""
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from desk_helpers import (
    DAX,
    DAX_ETF,
    NOW,
    FakeSc,
    daily_points,
    ko_item,
    make_user,
    standard_handler,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import calendar as cal
from app.decision.desk import prereg, service
from app.decision.desk.guard import DeskState, Proposal, check_entry, check_exit
from app.decision.desk.ledger import append_event, verify_chain
from app.decision.desk.limits import LIMITS
from app.decision.desk.products import select_product
from app.decision.desk.scoring import Trade, holm, score_cell
from app.decision.desk.signals import index_momentum, owner_calls
from app.decision.desk.sizing import size_position, stop_fraction
from app.decision.desk.underlyings import (
    ALIASES,
    UnderlyingUnknown,
    daily_sigma,
    resolve_underlying,
)
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskEvent, DeskPosition, TrialLedgerEntry


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


UTC = ZoneInfo("UTC")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Earnings and news lookups reach providers in production; the desk tests stub them."""
    from app.decision.desk import news

    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)
    monkeypatch.setattr(news, "alpaca_for", lambda db: None)


# --- limits -----------------------------------------------------------------------


def test_limits_are_pinned():
    assert LIMITS.as_json() == {
        "pot": "250", "daily_loss": "15", "max_loss_per_trade": "5", "fee_per_order": "0.99",
        "max_open_live": 1, "max_entries_per_day": 2, "max_holding_trading_days": 2,
        "cutoff_hour": 21, "cutoff_minute": 30, "window_start_hour": 8, "window_start_minute": 5,
        "window_end_hour": 21, "window_end_minute": 45, "warrant_min_days_to_expiry": 21,
        "knockout_max_leverage": "5", "knockout_min_barrier_sigmas": "3", "warrant_min_abs_delta": "0.3",
        "warrant_max_abs_delta": "0.7", "spread_cap_liquid": "0.01", "spread_cap_other": "0.025",
        "max_quote_age_s": 60, "tap_window_s": 180, "reask_move": "0.01", "quote_lost_s": 600,
        "stop_min_fraction": "0.03", "stop_max_fraction": "0.30", "stop_sigmas": "1",
    }


def test_limits_are_not_settings():
    from app.foundation.settings import DEFAULT_PUBLIC_SETTINGS

    assert not [k for k in DEFAULT_PUBLIC_SETTINGS if k.startswith("desk_") and "limit" in k]
    with pytest.raises(Exception):
        LIMITS.pot = Decimal(1)  # frozen


# --- calendar ---------------------------------------------------------------------


def test_cutoff_friday_and_holiday_and_dst():
    assert cal.flat_by_day(datetime(2026, 11, 20).date())  # Friday
    assert not cal.flat_by_day(datetime(2026, 11, 18).date())  # Wednesday
    assert not cal.is_trading_day(datetime(2026, 12, 25).date())  # a Xetra holiday
    assert cal.flat_by_day(datetime(2026, 12, 23).date())  # Dec 24 is closed
    # Never held over a weekend: Thursday entry ends Friday 21:30, Friday entry ends the same evening.
    assert cal.max_exit_at(datetime(2026, 11, 19, 10, tzinfo=cal.BERLIN)).date().isoformat() == "2026-11-20"
    assert cal.max_exit_at(datetime(2026, 11, 20, 10, tzinfo=cal.BERLIN)).date().isoformat() == "2026-11-20"
    # Wednesday with one trading day ("today"): the same evening.
    assert cal.max_exit_at(NOW, days=1).date().isoformat() == "2026-11-18"
    # DST: NYSE closes at 16:00 New York = 22:00 Berlin in the weeks both are on summer or winter time,
    # but 21:00 Berlin when the US is already on summer time and Europe is not (March 2026).
    us_close = cal.session_times(datetime(2026, 3, 16).date(), "XNYS")[1].astimezone(cal.BERLIN)
    assert us_close.hour == 21
    us_close_nov = cal.session_times(datetime(2026, 11, 18).date(), "XNYS")[1].astimezone(cal.BERLIN)
    assert us_close_nov.hour == 22


def test_macro_calendar_blocks_known_dates():
    assert cal.macro_events_between(datetime(2026, 12, 9).date(), datetime(2026, 12, 10).date()) == [
        ("fomc", "2026-12-09"), ("cpi", "2026-12-10")]
    assert cal.macro_calendar_covers(2027)


# --- guard ------------------------------------------------------------------------


def _proposal(**kw):
    base = dict(signal="owner_call", product_type="knockout", units=10, ask=Decimal("1"), notional=Decimal("10"),
                worst_case_loss=Decimal("4.98"), spread_pct=Decimal("0.005"), liquid=True, quote_age_s=10,
                exchange="XETR")
    base.update(kw)
    return Proposal(**base)


def test_guard_passes_a_clean_proposal():
    v = check_entry(DeskState(), _proposal(), NOW)
    assert v.ok and v.paper_ok and v.reasons == ()


@pytest.mark.parametrize("kw, reason", [
    (dict(worst_case_loss=Decimal("5.01")), "risk_over_limit"),
    (dict(quote_age_s=61), "quote_stale"),
    (dict(spread_pct=Decimal("0.0101")), "spread_too_wide"),
    (dict(units=0), "too_small"),
    (dict(product_type="warrant", days_to_expiry=20), "warrant_too_near_expiry"),
    (dict(earnings_in_window=True), "event_block_earnings"),
    (dict(is_index=True, macro_events=(("fomc", "2026-11-18"),)), "event_block_macro"),
])
def test_guard_hard_reasons_stop_paper_too(kw, reason):
    v = check_entry(DeskState(), _proposal(**kw), NOW)
    assert reason in v.reasons and not v.paper_ok and not v.ok


def test_guard_boundaries_are_inclusive_where_the_limit_is():
    ok = check_entry(DeskState(), _proposal(worst_case_loss=Decimal("5"), quote_age_s=60,
                                            spread_pct=Decimal("0.01")), NOW)
    assert ok.ok
    assert check_entry(DeskState(), _proposal(product_type="warrant", days_to_expiry=21), NOW).ok
    assert check_entry(DeskState(), _proposal(spread_pct=Decimal("0.025"), liquid=False), NOW).ok
    assert not check_entry(DeskState(), _proposal(spread_pct=Decimal("0.0251"), liquid=False), NOW).ok
    assert check_entry(DeskState(), _proposal(earnings_in_window=True, event_flag=True), NOW).ok


def test_guard_live_caps_do_not_stop_paper():
    for state, reason in [
        (DeskState(open_live=1), "max_open"), (DeskState(entries_today=2), "max_entries"),
        (DeskState(loss_today=Decimal("15")), "daily_loss"), (DeskState(pot_used=Decimal("245")), "pot_exceeded"),
    ]:
        v = check_entry(state, _proposal(), NOW)
        assert reason in v.reasons and v.paper_ok and not v.ok
    just_under = check_entry(DeskState(entries_today=1, loss_today=Decimal("14.99"), pot_used=Decimal("240")),
                             _proposal(), NOW)
    assert just_under.ok


def test_guard_halt_window_and_cutoff():
    assert "halted" in check_entry(DeskState(halted=True), _proposal(), NOW).reasons
    early = datetime(2026, 11, 18, 8, 4, tzinfo=cal.BERLIN)
    late = datetime(2026, 11, 18, 21, 46, tzinfo=cal.BERLIN)
    us_entry = datetime(2026, 11, 18, 21, 30, 5, tzinfo=cal.BERLIN)  # the US rule's entry, Mon-Thu: inside
    assert "outside_window_rule" not in check_entry(DeskState(), _proposal(signal="index_momentum"), us_entry).reasons
    assert "outside_window" in check_entry(DeskState(), _proposal(), early).reasons
    assert "outside_window" in check_entry(DeskState(), _proposal(), late).reasons
    assert "outside_window_rule" in check_entry(DeskState(), _proposal(signal="index_momentum"), late).reasons
    assert check_entry(DeskState(), _proposal(signal="index_momentum"), late).paper_ok  # live-only for the rule
    friday_late = datetime(2026, 11, 20, 21, 30, tzinfo=cal.BERLIN)
    assert "after_cutoff" in check_entry(DeskState(), _proposal(), friday_late).reasons
    saturday = datetime(2026, 11, 21, 11, tzinfo=cal.BERLIN)
    assert "outside_window" in check_entry(DeskState(), _proposal(), saturday).reasons
    assert "underlying_closed" in check_entry(DeskState(), _proposal(exchange="XNYS"), NOW).reasons


class _P:
    def __init__(self, **kw):
        self.stop_price = Decimal("0.95")
        self.max_exit_at = NOW + timedelta(hours=5)
        self.last_quote_at = NOW - timedelta(seconds=30)
        self.entry_at = NOW - timedelta(hours=1)
        self.__dict__.update(kw)


def test_check_exit_reasons():
    assert check_exit(_P(), Decimal("1.00"), NOW) is None
    assert check_exit(_P(), Decimal("0.95"), NOW) == "stop"
    assert check_exit(_P(), Decimal("0.96"), NOW) is None
    assert check_exit(_P(), Decimal("1.00"), NOW, halted=True) == "halt"
    assert check_exit(_P(max_exit_at=NOW), Decimal("1.00"), NOW) == "horizon"
    fri = datetime(2026, 11, 20, 21, 30, tzinfo=cal.BERLIN)
    assert check_exit(_P(max_exit_at=fri + timedelta(hours=3)), Decimal("1"), fri) == "friday_cutoff"
    eve = datetime(2026, 12, 23, 21, 30, tzinfo=cal.BERLIN)
    assert check_exit(_P(max_exit_at=eve + timedelta(hours=3)), Decimal("1"), eve) == "holiday_cutoff"
    assert check_exit(_P(last_quote_at=NOW - timedelta(minutes=10)), None, NOW) == "quote_lost"
    assert check_exit(_P(last_quote_at=NOW - timedelta(minutes=9)), None, NOW) is None


# --- sizing -----------------------------------------------------------------------


def test_sizing_formula_clamps_and_too_small():
    s = size_position(Decimal("1"), Decimal("5"), Decimal("0.01"), Decimal("250"))
    assert s.stop_fraction == Decimal("0.05") and s.stop_price == Decimal("0.9500")
    assert s.units == 60 and s.notional == Decimal("60") and s.worst_case_loss <= LIMITS.max_loss_per_trade
    assert stop_fraction(Decimal("1"), Decimal("0.001")) == Decimal("0.03")
    assert stop_fraction(Decimal("10"), Decimal("0.05")) == Decimal("0.30")
    assert size_position(Decimal("100"), Decimal("5"), Decimal("0.01"), Decimal("250")).reason == "too_small"
    capped = size_position(Decimal("1"), Decimal("5"), Decimal("0.01"), Decimal("20"))
    assert capped.units == 20  # the pot left caps the size


# --- ledger -----------------------------------------------------------------------


def test_ledger_chain_tamper_and_fork():
    db = _memory_db()
    for i in range(3):
        append_event(db, "error", "paper", {"i": i}, now=NOW + timedelta(seconds=i))
    db.commit()
    assert verify_chain(db) == (True, None)
    row = db.query(DeskEvent).filter(DeskEvent.seq == 2).one()
    row.payload = json.dumps({"i": 99})
    db.commit()
    assert verify_chain(db) == (False, 2)
    row.payload = json.dumps({"i": 1}, separators=(",", ":"))
    db.commit()
    assert verify_chain(db) == (True, None)
    # A fork (same prev_hash) fails on the unique constraint.
    from sqlalchemy.exc import IntegrityError

    first = db.query(DeskEvent).filter(DeskEvent.seq == 1).one()
    db.add(DeskEvent(seq=9, created_at=NOW, kind="error", mode="paper", payload="{}", prev_hash=first.prev_hash,
                     row_hash="f" * 64))
    with pytest.raises(IntegrityError):
        db.flush()


def test_ledger_rejects_unknown_kind_and_is_ascii():
    db = _memory_db()
    with pytest.raises(ValueError):
        append_event(db, "bogus", "paper", {})
    row = append_event(db, "error", "paper", {"name": "Straße €"}, now=NOW)
    assert row.payload.isascii()


# --- underlyings and volatility ---------------------------------------------------


def test_underlying_resolution():
    db = _memory_db()
    assert resolve_underlying(db, "dax").isin == DAX
    assert resolve_underlying(db, "DE000AAAAAA1").isin == "DE000AAAAAA1"
    with pytest.raises(UnderlyingUnknown):
        resolve_underlying(db, "NOPE")
    assert set(ALIASES) == {"DAX", "ES50", "SPX", "NDX"}


def test_daily_sigma_needs_enough_closes():
    assert daily_sigma([100] * 10) is None
    closes = [100 + (i % 2) for i in range(30)]
    assert 0.005 < float(daily_sigma(closes)) < 0.02


# --- products ---------------------------------------------------------------------


def _ko_handler(extra_quotes=None):
    quotes = {"DE000KO000A1": ("0.995", "1.00"), "DE000KO000A2": ("0.90", "1.00"), "DE000KO000A3": ("0.995", "1.00"),
              **(extra_quotes or {})}
    listings = {"knockout": [
        ko_item("DE000KO000A1", lev=4, distance=0.25),
        ko_item("DE000KO000A2", lev=4, distance=0.25),  # 10 % spread: dropped
        ko_item("DE000KO000A3", lev=4, distance=0.02),  # barrier too close
        ko_item("DE000KO000A4", lev=9, distance=0.5),   # leverage over the cap
    ]}
    return standard_handler(quotes, listings)


def test_select_product_filters_quotes_and_ranks():
    sc = FakeSc(_ko_handler())
    sel = select_product(sc.cli(), DAX, "long", "knockout", NOW, sigma=Decimal("0.01"), liquid=True)
    assert [c.isin for c in sel.candidates] == ["DE000KO000A1"]
    reasons = {d["isin"]: d["reason"] for d in sel.dropped}
    assert reasons == {"DE000KO000A2": "spread_too_wide", "DE000KO000A3": "barrier_too_close",
                       "DE000KO000A4": "leverage"}
    searched = next(c for c in sc.calls if c[:3] == ["broker", "derivatives", "search"])
    assert "--leverage-max" in searched and "leverage" in searched and "desc" in searched


def test_select_product_drops_stale_outdated_and_missing_quotes():
    def handler(args):
        if args[:2] == ["broker", "quote"]:
            from desk_helpers import quote_result

            isin = args[3]
            if isin == "DE000KO000A1":
                return {"result": quote_result(isin, "0.99", "1.00", age=61)}
            if isin == "DE000KO000A2":
                return {"result": quote_result(isin, "0.99", "1.00", outdated=True)}
            return {"result": {"isin": isin, "quote_bid_price": None, "quote_ask_price": None}}
        return standard_handler({}, {"knockout": [ko_item(f"DE000KO000A{i}") for i in (1, 2, 3)]})(args)
    sel = select_product(FakeSc(handler).cli(), DAX, "long", "knockout", NOW, sigma=Decimal("0.01"), liquid=True)
    assert sel.candidates == []
    assert {d["reason"] for d in sel.dropped} == {"quote_stale", "no_quote"}


def test_warrant_direction_mapping_and_delta_band():
    from app.decision.desk.products import strategy_for

    assert [strategy_for("long", "knockout"), strategy_for("short", "knockout"),
            strategy_for("long", "warrant"), strategy_for("short", "warrant")] == ["long", "short", "call", "put"]
    epoch = (NOW.date() + timedelta(days=60) - datetime(1970, 1, 1).date()).days
    near = (NOW.date() + timedelta(days=10) - datetime(1970, 1, 1).date()).days
    base = {"strategy": "put", "issuer": "X", "omega": 8, "implied_volatility": 0.2, "underlying_isin": DAX}
    items = [
        {**base, "isin": "DE000WA000A1", "delta": -0.5, "expiry_date": {"epoch_day": epoch}},
        {**base, "isin": "DE000WA000A2", "delta": -0.9, "expiry_date": {"epoch_day": epoch}},
        {**base, "isin": "DE000WA000A3", "delta": -0.5, "expiry_date": {"epoch_day": near}},
    ]
    sc = FakeSc(standard_handler({"DE000WA000A1": ("0.995", "1.00")}, {"warrant": items}))
    sel = select_product(sc.cli(), DAX, "short", "warrant", NOW, sigma=Decimal("0.01"), liquid=True)
    assert [c.isin for c in sel.candidates] == ["DE000WA000A1"]
    assert sel.candidates[0].realised_vol is not None and sel.candidates[0].implied_vol == Decimal("0.2")
    assert {d["reason"] for d in sel.dropped} == {"delta_band", "near_expiry"}


# --- owner calls ------------------------------------------------------------------


def test_parse_idea():
    i = owner_calls.parse_idea("/idea DAX up 1d ko")
    assert (i.target, i.direction, i.horizon, i.product_types, i.event) == ("DAX", "long", "1d", ("knockout",), False)
    j = owner_calls.parse_idea("/idea US0000000001 down event")
    assert j.direction == "short" and j.product_types == ("knockout", "warrant") and j.event
    for bad in ("/idea", "/idea DAX", "/idea DAX sideways", "/idea DAX up banana", "/idea DAX up 2d"):
        with pytest.raises(owner_calls.IdeaParseError):
            owner_calls.parse_idea(bad)


def _setup_idea(db, quotes=None):
    user = make_user(db)
    q = {"DE000KO000A1": ("0.995", "1.00"), DAX_ETF: ("100", "100.1")}
    q.update(quotes or {})
    sc = FakeSc(standard_handler(q, {"knockout": [ko_item("DE000KO000A1", lev=4, distance=0.25)]},
                                 chart_pts=daily_points()))
    return user, sc


def test_idea_proposes_then_tap_fills_at_a_fresh_quote_once():
    db = _memory_db()
    user, sc = _setup_idea(db)
    outcome = owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea DAX up today ko"), sc.cli(), NOW)
    db.commit()
    res = outcome.proposals[0]
    assert res.position.status == "proposed" and res.nonce and res.position.cell == "owner_call:knockout"
    assert "Paper: take" in json.dumps(owner_calls.take_markup(res.position.id, res.nonce))
    # Tap within the window, quote unchanged: filled.
    tap = service.take(db, res.position.id, res.nonce, sc.cli(), NOW + timedelta(seconds=30), outcome.underlying)
    db.commit()
    assert tap.status == "filled" and res.position.status == "open" and res.position.entry_fee == LIMITS.fee_per_order
    kinds = [e.kind for e in db.query(DeskEvent).order_by(DeskEvent.seq)]
    assert kinds[:5] == ["candidates", "guard_verdict", "proposal", "tap", "fill"] and "stop_set" in kinds
    assert verify_chain(db) == (True, None)
    # The underlying's entry price is stored on a tap, so the placebo gets a real move.
    assert res.position.underlying_entry is not None
    # A replay is refused and logged.
    again = service.take(db, res.position.id, res.nonce, sc.cli(), NOW + timedelta(seconds=31))
    assert again.status == "replay"


def test_tap_after_the_window_expires_and_wrong_nonce_is_unknown():
    db = _memory_db()
    user, sc = _setup_idea(db)
    res = owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea DAX up ko"), sc.cli(), NOW).proposals[0]
    db.commit()
    assert service.take(db, res.position.id, "bad", sc.cli(), NOW).status == "unknown"
    late = service.take(db, res.position.id, res.nonce, sc.cli(), NOW + timedelta(seconds=181))
    assert late.status == "expired" and res.position.status == "expired"


def test_a_halt_between_proposal_and_tap_refuses_the_fill():
    db = _memory_db()
    user, sc = _setup_idea(db)
    outcome = owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea DAX up ko"), sc.cli(), NOW)
    res = outcome.proposals[0]
    db.commit()
    service.set_halt(db, True, "test", "test", now=NOW + timedelta(seconds=5))
    tap = service.take(db, res.position.id, res.nonce, sc.cli(), NOW + timedelta(seconds=30), outcome.underlying)
    db.commit()
    assert tap.status == "refused" and "halted" in tap.message and res.position.status == "rejected"
    assert res.position.entry_at is None
    kinds = [e.kind for e in db.query(DeskEvent).order_by(DeskEvent.seq)]
    assert "fill" not in kinds and kinds[-1] == "error" and verify_chain(db) == (True, None)


def test_tap_reasks_when_the_ask_moved_more_than_one_percent():
    db = _memory_db()
    user, sc = _setup_idea(db)
    res = owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea DAX up ko"), sc.cli(), NOW).proposals[0]
    db.commit()
    moved = FakeSc(standard_handler({"DE000KO000A1": ("1.01", "1.02")}))
    tap = service.take(db, res.position.id, res.nonce, moved.cli(), NOW + timedelta(seconds=30))
    assert tap.status == "reask" and tap.nonce and tap.nonce != res.nonce and res.position.status == "proposed"
    ok = service.take(db, res.position.id, tap.nonce, moved.cli(), NOW + timedelta(seconds=40))
    assert ok.status == "filled" and res.position.entry_price == Decimal("1.02")


def test_untapped_proposals_expire_and_do_not_count():
    db = _memory_db()
    user, sc = _setup_idea(db)
    res = owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea DAX up ko"), sc.cli(), NOW).proposals[0]
    db.commit()
    assert service.expire_proposals(db, NOW + timedelta(seconds=100)) == 0
    assert service.expire_proposals(db, NOW + timedelta(seconds=200)) == 1
    assert res.position.status == "expired"


def test_halt_blocks_a_new_idea_and_closes_open_positions():
    db = _memory_db()
    user, sc = _setup_idea(db)
    res = owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea DAX up ko"), sc.cli(), NOW).proposals[0]
    service.take(db, res.position.id, res.nonce, sc.cli(), NOW + timedelta(seconds=10))
    service.set_halt(db, True, "test", "test", now=NOW)
    nxt = owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea DAX up ko"), sc.cli(), NOW)
    assert "halted" in (nxt.proposals[0].reason or "")
    closed = service.manage_open_positions(db, sc.cli(), NOW + timedelta(minutes=5), {d.isin: d for d in ALIASES.values()})
    db.commit()
    assert [r for _, r in closed] == ["halt"] and res.position.status == "closed"
    assert res.position.net_eur is not None


def test_a_stop_hit_closes_at_the_bid_with_both_fees():
    db = _memory_db()
    user, sc = _setup_idea(db)
    res = owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea DAX up ko"), sc.cli(), NOW).proposals[0]
    service.take(db, res.position.id, res.nonce, sc.cli(), NOW + timedelta(seconds=10))
    pos = res.position
    crash = FakeSc(standard_handler({"DE000KO000A1": ("0.80", "0.81"), DAX_ETF: ("95", "95.1")},
                                    now=NOW + timedelta(minutes=5)))
    closed = service.manage_open_positions(db, crash.cli(), NOW + timedelta(minutes=5), {d.isin: d for d in ALIASES.values()})
    assert [r for _, r in closed] == ["stop"]
    expected = (Decimal("0.80") - Decimal("1.00")) * pos.units - 2 * LIMITS.fee_per_order
    assert pos.net_eur == expected.quantize(Decimal("0.0001"))


def test_unknown_underlying_asks_for_the_isin():
    db = _memory_db()
    user, sc = _setup_idea(db)
    out = owner_calls.start_idea(db, user.id, owner_calls.parse_idea("/idea ZZZZ up"), sc.cli(), NOW)
    assert out.error and "ISIN" in out.error and out.proposals == []


# --- the index rule ---------------------------------------------------------------


def _pts(day_values: dict, tz=cal.BERLIN):
    return [(datetime.fromisoformat(k).replace(tzinfo=tz).astimezone(UTC), Decimal(str(v))) for k, v in day_values.items()]


def test_r1_xetra_from_fixture_bars():
    pts = _pts({"2026-11-17T17:30": 100, "2026-11-17T17:00": 99, "2026-11-17T21:00": 150,
                "2026-11-18T09:30": 101})
    r1 = index_momentum.compute_r1_xetra(pts, datetime(2026, 11, 18).date())
    assert r1 == Decimal("0.01")  # 101 / 100 - 1, the 21:00 evening print is not the close
    assert index_momentum.compute_r1_xetra(pts[:3], datetime(2026, 11, 18).date()) is None


def test_r1_us_from_fixture_bars():
    ny = cal.NEW_YORK
    bars = [{"t": datetime(2026, 11, 17, 15, 30, tzinfo=ny), "c": Decimal(100)},
            {"t": datetime(2026, 11, 18, 9, 30, tzinfo=ny), "c": Decimal(99)}]
    assert index_momentum.compute_r1_us(bars, datetime(2026, 11, 18).date()) == Decimal("-0.01")


def test_skip_rules():
    plans = {p.key: p for p in index_momentum.plans_for_day(datetime(2026, 11, 18).date())}
    assert index_momentum.skip_reason(plans["DAX"]) is None
    fri = {p.key: p for p in index_momentum.plans_for_day(datetime(2026, 11, 20).date())}
    assert index_momentum.skip_reason(fri["SPX"]) == "cutoff_before_exit"
    assert index_momentum.skip_reason(fri["DAX"]) is None  # 17:29 Berlin is before the 21:30 cut-off
    macro = {p.key: p for p in index_momentum.plans_for_day(datetime(2026, 12, 9).date())}
    assert index_momentum.skip_reason(macro["DAX"]) == "macro_event"
    half = {p.key: p for p in index_momentum.plans_for_day(datetime(2026, 11, 27).date())}
    assert index_momentum.skip_reason(half["SPX"]) == "half_day"


def test_index_signal_fires_once_opens_both_products_and_skips_the_rest():
    db = _memory_db()
    user = make_user(db)
    enter = index_momentum.plans_for_day(datetime(2026, 11, 18).date())[0].enter_at  # DAX 17:00 Berlin
    assert enter.astimezone(cal.BERLIN).hour == 17
    day_pts = [{"mid_price": 100, "timestamp_utc": "2026-11-17T16:30:00Z"},
               {"mid_price": 101, "timestamp_utc": "2026-11-18T08:30:00Z"}]

    def handler(args):
        if args[:2] == ["broker", "chart"] and args[args.index("--timeframe") + 1] == "7d":
            return {"data_points": day_pts}
        return std(args)

    from app.decision.desk.underlyings import ALIASES as A

    ko = ko_item("DE000KO000A1", lev=4, distance=0.25)
    wi = {"isin": "DE000WA000A1", "strategy": "call", "issuer": "X", "omega": 8, "delta": 0.5,
          "implied_volatility": 0.2, "underlying_isin": A["DAX"].isin,
          "expiry_date": {"epoch_day": (datetime(2027, 1, 31).date() - datetime(1970, 1, 1).date()).days}}
    std = standard_handler({"DE000KO000A1": ("0.995", "1.00"), "DE000WA000A1": ("0.995", "1.00"),
                            DAX_ETF: ("100", "100.1"), "IE0008471009": ("50", "50.1")},
                           {"knockout": [ko], "warrant": [wi]}, now=enter + timedelta(seconds=5))
    sc = FakeSc(handler)
    pass

    fired = index_momentum.fire_due_signals(db, user.id, sc.cli(), enter + timedelta(seconds=5))
    db.commit()
    assert any(k.startswith("DAX:") for k in fired)
    rows = db.query(DeskPosition).filter(DeskPosition.signal == "index_momentum").all()
    assert {r.product_type for r in rows if r.status == "open"} == {"knockout", "warrant", "cfd"}
    assert all(r.direction == "long" for r in rows)
    assert all(r.max_exit_at.astimezone(cal.BERLIN).minute == 27 for r in rows if r.status == "open")
    count = db.query(DeskPosition).count()
    assert index_momentum.fire_due_signals(db, user.id, sc.cli(), enter + timedelta(seconds=30)) == []
    assert db.query(DeskPosition).count() == count  # once per index and day
    signals = [json.loads(e.payload) for e in db.query(DeskEvent).filter(DeskEvent.kind == "signal")]
    assert [s["key"].split(":")[0] for s in signals] == ["DAX", "ES50"]


# --- scoring ----------------------------------------------------------------------


def _trades(nets, move=0.001):
    return [Trade(day=f"2026-12-{(i % 20) + 1:02d}", net=n, notional=60.0, leverage=5.0, move=move, cost=2.2)
            for i, n in enumerate(nets)]


def test_scoring_zero_edge_fails_and_large_edge_passes():
    import random

    rnd = random.Random(1)
    zero = score_cell(_trades([rnd.choice([-1.0, 1.0]) for _ in range(60)]))
    assert zero["n"] == 60 and zero["p"] > 0.05
    strong = score_cell(_trades([2.0 + rnd.random() for _ in range(60)], move=0.01))
    assert strong["p"] < 0.01 and strong["mean_net"] > strong["placebo_p95"]
    assert score_cell(_trades([5.0] * 39))["p"] == 1.0  # under n_min: untested
    assert score_cell([])["n"] == 0


def test_scoring_leaves_a_few_missing_moves_out_of_the_placebo_but_not_many():
    trades = _trades([2.0] * 60, move=0.01)
    trades[0] = replace(trades[0], move=None)  # one failed price read must not void the cell for good
    scored = score_cell(trades)
    assert scored["missing_move"] == 1 and scored["placebo_p95"] is not None and scored["p"] < 0.05
    many = [replace(t, move=None) if i < 7 else t for i, t in enumerate(trades)]  # 7 of 60: over 10 %
    scored = score_cell(many)
    assert scored["missing_move"] == 7 and scored["placebo_p95"] is None


def test_scoring_is_deterministic():
    t = _trades([1.0, -0.5, 2.0, 0.3] * 15)
    assert score_cell(t) == score_cell(t)


def test_holm_ordering():
    adj = holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": 1.0})
    assert adj["a"] == pytest.approx(0.04) and adj["c"] == pytest.approx(0.09)
    assert adj["b"] == pytest.approx(0.09) and adj["d"] == 1.0  # monotone: never below an earlier adjusted p


def test_scorecard_counts_only_positions_after_registration():
    db = _memory_db()
    user = make_user(db)
    reg = prereg.register(db, now=NOW)
    for i, offset in enumerate((-2, 1, 2)):
        db.add(DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                            isin="DE000KO000A1", underlying_isin=DAX, direction="long", status="closed", units=10,
                            entry_price=Decimal(1), entry_fee=Decimal("0.99"), exit_fee=Decimal("0.99"),
                            entry_at=reg.registered_at + timedelta(days=offset), net_eur=Decimal("1.5"),
                            leverage_or_omega=Decimal(5), underlying_move=Decimal("0.01")))
    db.add(DeskPosition(user_id=user.id, cell="external", signal="external", product_type="unknown", isin="X",
                        underlying_isin="", direction="long", status="closed",
                        entry_at=reg.registered_at + timedelta(days=1)))
    db.commit()
    from app.decision.desk.scoring import scorecard

    card = scorecard(db)
    assert card["cells"]["owner_call:knockout"]["n"] == 2 and card["external_closed"] == 1
    assert card["cells"]["owner_call:knockout"]["passes"] is False


# --- pre-registration -------------------------------------------------------------


def test_prereg_hash_is_pinned():
    assert prereg.PREREG_HASH == "86396b951089aa96007d6227bbfda59991be23b7b8b8ecd2975af6afa5c4a8cd"
    assert prereg.CELLS == ("owner_call:knockout", "owner_call:warrant", "index_momentum:knockout",
                            "index_momentum:warrant", "index_momentum:cfd")


def test_prereg_registers_once_and_records_one_trial_per_cell():
    db = _memory_db()
    prereg.register(db, now=NOW)
    prereg.register(db, now=NOW + timedelta(days=1))
    rows = db.query(TrialLedgerEntry).filter(TrialLedgerEntry.context == "desk").all()
    assert len(rows) == 5 and len({r.config_hash for r in rows}) == 5  # 5 trials under ADR 0025, not 1
    assert db.query(DeskEvent).filter(DeskEvent.kind == "registered").count() == 1
    assert prereg.registered_at(db) == NOW.astimezone(UTC)


# --- reconcile --------------------------------------------------------------------


def test_reconcile_logs_an_external_trade_once():
    from app.decision.desk.jobs import reconcile_external
    from app.foundation.models.entities import ActivityLedgerEntry, DerivativeTerms

    db = _memory_db()
    user = make_user(db)
    db.add(DerivativeTerms(user_id=user.id, isin="DE000KO000A1", kind="put", strike=Decimal(1), ratio=Decimal(1),
                           expiry=NOW.date() + timedelta(days=30), underlying_symbol="X"))
    db.add(ActivityLedgerEntry(user_id=user.id, source="scalable", dedupe_hash="h1", activity_type="buy",
                               date=NOW.date(), description="Fake put", isin="DE000KO000A1",
                               quantity=Decimal(10), price=Decimal("1.2")))
    db.commit()
    assert reconcile_external(db, NOW) == 1
    assert reconcile_external(db, NOW) == 0
    pos = db.query(DeskPosition).one()
    assert pos.signal == "external" and pos.cell == "external"
    assert db.query(DeskEvent).filter(DeskEvent.kind == "external_trade").count() == 1


# --- the monitor tick -------------------------------------------------------------


def test_monitor_tick_skips_outside_hours_and_flags_an_outdated_wrapper():
    from app.decision.desk.jobs import monitor_tick

    db = _memory_db()
    make_user(db)
    assert monitor_tick(db, datetime(2026, 11, 21, 11, tzinfo=cal.BERLIN))["skipped"] == "outside_hours"
    import subprocess

    def refuse(argv, **kw):
        out = {"ok": False, "command": "q", "error": {"code": "command_not_allowed", "message": "no"}, "hints": []}
        return subprocess.CompletedProcess(argv, 10, json.dumps(out).encode(), b"")

    from app.foundation.scalable.cli import ScalableCli, ScalableCliConfig

    cli = ScalableCli(ScalableCliConfig(wrapper_path="/fake/quantfolio-sc-ro", cli_user="", max_attempts=1,
                                        trusted_wrapper_only=False), runner=refuse)
    # Tick at 17:00 Berlin on a Wednesday so a DAX signal tries to read the chart.
    result = monitor_tick(db, datetime(2026, 11, 18, 17, 0, 5, tzinfo=cal.BERLIN), cli)
    assert result["skipped"] == "wrapper_outdated" and service.health(db)["wrapper_outdated"] is True


def test_monitor_tick_logs_a_scalable_error_once_not_every_minute():
    import subprocess

    from app.decision.desk.jobs import monitor_tick
    from app.foundation.scalable.cli import ScalableCli, ScalableCliConfig

    db = _memory_db()
    make_user(db)

    def expired(argv, **kw):
        out = {"ok": False, "command": "q", "error": {"code": "session_expired", "message": "no"}, "hints": []}
        return subprocess.CompletedProcess(argv, 20, json.dumps(out).encode(), b"")

    cli = ScalableCli(ScalableCliConfig(wrapper_path="/fake/quantfolio-sc-ro", cli_user="", max_attempts=1,
                                        trusted_wrapper_only=False), runner=expired)
    for minute in range(3):
        monitor_tick(db, datetime(2026, 11, 18, 17, minute, 5, tzinfo=cal.BERLIN), cli)
    db.commit()
    assert db.query(DeskEvent).filter(DeskEvent.kind == "error").count() == 1


def test_no_order_words_in_the_desk_package():
    """Phase 1 has no write path: no order word in any desk or market source."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    sources = list((root / "decision" / "desk").rglob("*.py")) + [root / "foundation" / "scalable" / "market.py"]
    pattern = re.compile(r'"(trade|buy|sell|cancel|--confirm|--acknowledge-appropriateness-warning)"')
    for path in sources:
        text_ = path.read_text()
        if path.name == "jobs.py":  # the reconcile reads the ledger's own "buy"/"sell" activity types
            text_ = text_.replace('("buy", "sell")', "")
        assert not pattern.search(text_), path
    from app.foundation.scalable.cli import ALLOWED_COMMANDS, MUTATING_WORDS

    assert MUTATING_WORDS == frozenset({"login", "logout", "select", "trade", "buy", "sell", "cancel", "add", "remove",
                                        "create", "update", "delete", "assign", "unassign", "config", "--confirm"})
    assert not any("trade" in path for path in ALLOWED_COMMANDS)


def test_desk_events_reject_update_on_postgres_only_by_trigger_is_declared_in_the_migration():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0157_derivatives_desk.py").read_text()
    assert "BEFORE UPDATE OR DELETE ON desk_events" in src and "append-only" in src
