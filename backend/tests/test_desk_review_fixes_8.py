"""Regression tests for the eighth 2026-11-20 review of the derivatives desk (/code-review)."""
import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from desk_helpers import DAX_ETF, NOW, FakeSc, ko_item, make_user, standard_handler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import calendar as cal
from app.decision.desk import jobs, news, newswire, service
from app.decision.desk.ledger import append_event
from app.decision.desk.signals import index_momentum
from app.decision.desk.underlyings import ALIASES
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskEvent, DeskPosition
from app.lab import desk_replay

REAL_HEADLINES = news.headlines  # before the autouse fixture replaces it
KO = "DE000KO000A1"
WA = "DE000WA000A1"


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)


ENTER = index_momentum.plans_for_day(date(2026, 11, 18))[0].enter_at  # DAX and ES50, 17:00 Berlin
DAY_PTS = [{"mid_price": 100, "timestamp_utc": "2026-11-17T16:30:00Z"},
           {"mid_price": 101, "timestamp_utc": "2026-11-18T08:30:00Z"}]
WARRANT = {"isin": WA, "strategy": "call", "issuer": "X", "omega": 8, "delta": 0.5, "implied_volatility": 0.2,
           "underlying_isin": ALIASES["DAX"].isin, "expiry_date": {"epoch_day": 20819}}


def _rule_sc(at, log=None, refuse_search=False):
    std = standard_handler({KO: ("0.995", "1.00"), WA: ("0.995", "1.00"), DAX_ETF: ("100", "100.1"),
                            "IE0008471009": ("50", "50.1")},
                           {"knockout": [ko_item(KO, lev=4, distance=0.25)], "warrant": [WARRANT]}, now=at)

    def handle(args):
        if log is not None:
            log.append("read")
        if args[:2] == ["broker", "chart"] and args[args.index("--timeframe") + 1] == "7d":
            return {"data_points": DAY_PTS}
        return std(args)

    class Sc(FakeSc):
        def __call__(self, argv, **kw):
            args = [a for a in argv[1:] if a != "--json"]
            if refuse_search and args[:3] == ["broker", "derivatives", "search"]:
                out = {"ok": False, "command": "q", "error": {"code": "session_expired", "message": "no"},
                       "hints": []}
                return subprocess.CompletedProcess(argv, 20, json.dumps(out).encode(), b"")
            return super().__call__(argv, **kw)

    return Sc(handle)


# 1. The index rule makes every network read before its first write.
def test_the_index_rule_reads_everything_before_its_first_write(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    log: list[str] = []
    real = index_momentum.append_event
    monkeypatch.setattr(index_momentum, "append_event", lambda *a, **k: log.append("write") or real(*a, **k))
    real_svc = service.append_event
    monkeypatch.setattr(service, "append_event", lambda *a, **k: log.append("write") or real_svc(*a, **k))
    at = ENTER + timedelta(seconds=5)
    fired = index_momentum.fire_due_signals(db, user.id, _rule_sc(at, log).cli(), at)
    assert sorted(fired) == ["DAX:2026-11-18", "ES50:2026-11-18"]
    assert "read" not in log[log.index("write"):]  # both indices read, then written
    opened = db.query(DeskPosition).filter(DeskPosition.status == "open").all()
    assert {p.product_type for p in opened} == {"knockout", "warrant", "cfd"}
    assert all(p.underlying_entry is not None for p in opened if p.product_type != "cfd")


# 3. A failed Scalable read for the products does not cancel the CFD.
def test_a_failed_product_search_still_sends_the_cfd():
    db = _memory_db()
    user = make_user(db)
    at = ENTER + timedelta(seconds=5)
    errors: list[str] = []
    opened: list[str] = []
    fired = index_momentum.fire_due_signals(db, user.id, _rule_sc(at, refuse_search=True).cli(), at, opened, errors)
    assert "DAX:2026-11-18" in fired and "session_expired" in errors
    cfds = [db.get(DeskPosition, i) for i in opened]
    assert any(p.product_type == "cfd" and p.underlying_isin == ALIASES["DAX"].isin for p in cfds)
    failed = [json.loads(e.payload) for e in db.query(DeskEvent).filter(DeskEvent.kind == "guard_verdict")
              if '"read_failed"' in e.payload]
    assert {f["product_type"] for f in failed} == {"knockout", "warrant"}


# 2. A suffixed holding is never matched by its base ticker.
def test_a_xetra_holding_is_not_matched_by_an_unrelated_us_ticker(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    monkeypatch.setattr("app.foundation.live_positions.combined_positions", lambda db_, uid: [
        SimpleNamespace(isin="DE0008404005", ticker="ALV.DE", name="Allianz"),
        SimpleNamespace(isin="US0378331005", ticker="AAPL", name="Apple")])
    monkeypatch.setattr("app.foundation.broker_instruments.us_ticker_for_isin", lambda db_, isin: None)
    watch = newswire.watchlist(db, user.id)
    assert "ALV" not in watch and watch["DE0008404005"] == "holding:ALV.DE" and watch["AAPL"] == "holding:AAPL"


# 4. A halt without a fresh bid waits for one, then closes at the last bid.
def test_a_halt_without_a_fresh_quote_waits_before_closing_at_the_last_bid():
    db = _memory_db()
    user = make_user(db)
    p = DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                     isin=KO, underlying_isin=ALIASES["DAX"].isin, direction="long", status="open", units=1,
                     entry_price=Decimal(1), entry_fee=Decimal("0.99"), last_bid=Decimal("0.95"),
                     entry_at=(NOW - timedelta(hours=1)).astimezone(UTC),
                     last_quote_at=(NOW - timedelta(minutes=2)).astimezone(UTC),
                     max_exit_at=(NOW + timedelta(hours=5)).astimezone(UTC))
    db.add(p)
    db.flush()
    append_event(db, "fill", service.MODE, {}, p.id, now=NOW)
    db.commit()
    service.set_halt(db, True, "sc down", "test", now=NOW)
    no_quote = FakeSc(standard_handler({}, now=NOW))
    assert service.manage_open_positions(db, no_quote.cli(), NOW, {}) == [] and p.status == "open"
    later = NOW + timedelta(minutes=9)
    closed = service.manage_open_positions(db, FakeSc(standard_handler({}, now=later)).cli(), later, {})
    assert [r for _, r in closed] == ["halt"] and p.exit_price == Decimal("0.95")


# 5. A pre-market SPY quote is not a knock-out.
class _Alpaca:
    def __init__(self, price, at):
        self.price, self.at = price, at

    def get_quote(self, symbol):
        return {"ok": True, "data": {"close": self.price}, "quality": {"as_of": self.at}}

    def get_intraday_bars(self, *a, **k):
        return []


def test_an_off_hours_spy_quote_does_not_knock_out_an_spx_product(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    p = DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                     isin=KO, underlying_isin=ALIASES["SPX"].isin, direction="long", status="open", units=1,
                     entry_price=Decimal(1), entry_at=datetime(2026, 11, 17, 20, 0, tzinfo=UTC))
    db.add(p)
    db.flush()
    append_event(db, "fill", service.MODE, {"barrier_ref": "600"}, p.id, now=NOW)
    db.commit()
    pre = datetime(2026, 11, 18, 8, 0, tzinfo=cal.NEW_YORK)
    monkeypatch.setattr(news, "alpaca_for", lambda db_: _Alpaca(Decimal("590"), pre))
    assert service._knocked_out_at(db, None, p, ALIASES["SPX"], pre) is None
    regular = datetime(2026, 11, 18, 10, 0, tzinfo=cal.NEW_YORK)
    monkeypatch.setattr(news, "alpaca_for", lambda db_: _Alpaca(Decimal("590"), regular))
    assert service._knocked_out_at(db, None, p, ALIASES["SPX"], regular) == Decimal("590")


# 6. A pending proposal on a US stock gets its Alpaca headlines, by ticker.
def test_the_news_check_covers_a_proposal_and_uses_its_ticker(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    tick = NOW.replace(minute=5)
    pending = DeskPosition(user_id=user.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                           isin=KO, underlying_isin="US67066G1040", underlying_label="NVIDIA", direction="long",
                           status="proposed")
    db.add(pending)
    db.flush()
    append_event(db, "proposal", service.MODE, {"proposed_at": tick.isoformat()}, pending.id, now=tick)
    db.commit()
    monkeypatch.setattr("app.decision.desk.underlyings.ticker_for_isin",
                        lambda db_, isin, uid=None: "NVDA" if isin == "US67066G1040" else None)
    asked: list[list[str]] = []

    class Alpaca:  # what the real headlines() asks Alpaca for (pullfrog: assert the query, not a field)
        def get_news(self, symbols, since):
            asked.append(list(symbols))
            return []

    monkeypatch.setattr(news, "headlines", REAL_HEADLINES)
    monkeypatch.setattr(news, "alpaca_for", lambda db_: Alpaca())
    jobs.monitor_tick(db, tick, FakeSc(standard_handler({})).cli())
    assert ["NVDA"] in asked


# 7. The replay skips the days the live rule skips.
def test_the_replay_skips_exactly_the_live_rules_flat_by_days():
    assert desk_replay.xetra_flat_by(date(2026, 11, 25)) is False  # before Thanksgiving: Xetra trades Thursday
    assert desk_replay.xetra_flat_by(date(2026, 4, 30)) is True  # 1 May: Xetra closed
    d = date(2026, 1, 1)
    while d < date(2027, 1, 1):
        assert desk_replay.xetra_flat_by(d) == cal.flat_by_day(d), d
        d += timedelta(days=1)
