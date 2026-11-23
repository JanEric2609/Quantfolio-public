"""Regression tests for the third 2026-11-20 review of the derivatives desk (two reports, merged).

Grouped by the pattern each finding belongs to: the underlying's price in the wrong units, freshness,
errors and messages tied to their own piece of work, and silent wrong answers.
"""
import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from desk_helpers import DAX_ETF, NOW, FakeSc, daily_points, ko_item, make_user, quote_result, standard_handler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import api_ops, cfd, jobs, news, newswire, service, telegram
from app.decision.desk import calendar as cal
from app.decision.desk.guard import check_exit
from app.decision.desk.ledger import append_event
from app.decision.desk.signals import index_momentum, owner_calls
from app.decision.desk.underlyings import ALIASES, Underlying, resolve_underlying
from app.foundation import desk_chain
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskEvent, DeskPosition, NewsWireItem
from app.foundation.scalable.cli import ScalableError


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


KO = "DE000KO000A1"
US_STOCK = Underlying("US0000000001", "Fake Corp", "XNYS", True)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(news, "headlines", lambda *a, **k: [])
    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: False)


def _ko_listing(barrier, distance, underlying_isin):
    item = ko_item(KO, lev=4, distance=distance)
    item["knockout_barrier"] = {"kind": "x", "value": barrier}
    item["underlying_isin"] = underlying_isin
    return item


def _handler(quotes, at, listings=None, path=None):
    """sc at time *at*: quotes are fresh at *at*; the 7-day chart is *path* (``[(ts, mid)]``)."""
    std = standard_handler(quotes, listings, chart_pts=daily_points(now=at), now=at)

    def handle(args):
        if args[:2] == ["broker", "chart"] and args[args.index("--timeframe") + 1] == "7d":
            pts = [{"mid_price": float(m), "timestamp_utc": t.astimezone(UTC).isoformat().replace("+00:00", "Z")}
                   for t, m in (path or [])]
            return {"data_points": pts}
        return std(args)
    return FakeSc(handle)


def _open_by_tap(db, user, underlying, quotes, barrier, distance, at):
    sc = _handler(quotes, at, {"knockout": [_ko_listing(barrier, distance, underlying.isin)]})
    res = service.propose(db, user.id, signal="owner_call", underlying=underlying, direction="long",
                          product_type="knockout", cli=sc.cli(), now=at)
    assert res.nonce, res.reason
    tap = service.take(db, res.position.id, res.nonce, sc.cli(), at + timedelta(seconds=10), underlying)
    assert tap.status == "filled", tap.message
    db.commit()
    return res.position


# -- 1. the underlying's price in the wrong units ------------------------------------------------


def test_a_dax_knockout_compares_its_barrier_in_the_etf_proxys_units():
    """Barrier 19,000 index points, proxy (DAX ETF) near 100 EUR, listing distance 25 %."""
    db = _memory_db()
    user = make_user(db)
    p = _open_by_tap(db, user, ALIASES["DAX"], {KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")}, 19000, 0.25, NOW)
    fill = json.loads(db.query(DeskEvent).filter(DeskEvent.kind == "fill").one().payload)
    assert Decimal(fill["barrier_ref"]) == (Decimal("100.05") * Decimal("0.75")).quantize(Decimal("0.000001"))
    # A missed product quote with the ETF at 100: the old check (100 <= 19,000) closed it at 0.
    t = NOW + timedelta(minutes=2)
    assert service.manage_open_positions(db, _handler({DAX_ETF: ("100", "100.1")}, t).cli(), t, {}) == []
    assert p.status == "open"
    # The ETF 30 % down: past the barrier.
    t2 = t + timedelta(minutes=1)
    closed = service.manage_open_positions(db, _handler({DAX_ETF: ("70", "70.1")}, t2).cli(), t2, {})
    assert [r for _, r in closed] == ["knock_out"] and p.exit_price == 0


def test_a_us_stock_knockout_is_judged_by_the_move_not_a_usd_barrier_against_a_eur_price():
    """Barrier 150 USD, 10 % away; the sc line is in EUR near 140. A 3.6 % fall is no knock-out."""
    db = _memory_db()
    user = make_user(db)
    at = NOW.replace(hour=16)
    p = _open_by_tap(db, user, US_STOCK, {KO: ("0.995", "1.00"), US_STOCK.isin: ("140", "140.1")}, 150, 0.10, at)
    t = at + timedelta(minutes=2)
    assert service.manage_open_positions(db, _handler({US_STOCK.isin: ("135", "135.1")}, t).cli(), t, {}) == []
    assert p.status == "open"


def test_a_touch_of_the_barrier_counts_even_after_the_level_recovers():
    db = _memory_db()
    user = make_user(db)
    p = _open_by_tap(db, user, ALIASES["DAX"], {KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")}, 19000, 0.25, NOW)
    t = NOW + timedelta(minutes=20)
    path = [(NOW + timedelta(minutes=5), Decimal("74")), (NOW + timedelta(minutes=10), Decimal("98"))]
    closed = service.manage_open_positions(db, _handler({DAX_ETF: ("100", "100.1")}, t, path=path).cli(), t, {})
    assert [r for _, r in closed] == ["knock_out"] and p.exit_price == 0 and p.net_eur < -9
    # A dip before the fill does not count.
    q = _open_by_tap(db, user, ALIASES["DAX"], {KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")}, 19000, 0.25,
                     NOW + timedelta(hours=1))
    t2 = NOW + timedelta(hours=1, minutes=5)
    early = [(NOW + timedelta(minutes=5), Decimal("60"))]
    assert service.manage_open_positions(db, _handler({DAX_ETF: ("100", "100.1")}, t2, path=early).cli(), t2, {}) == []
    assert q.status == "open"


class _FakeAlpaca:
    def __init__(self, price, as_of, bars):
        self.price, self.as_of, self.bars = price, as_of, bars

    def get_quote(self, symbol):
        return {"ok": True, "data": {"close": self.price}, "quality": {"as_of": self.as_of}}

    def get_intraday_bars(self, symbol, timeframe, start, end, **kw):
        return [b for b in self.bars if start <= b["t"] <= end]


def test_an_spx_short_knockout_reads_spys_highs_since_the_fill(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    at = NOW.replace(hour=16)
    p = DeskPosition(user_id=user.id, mode=service.MODE, signal="owner_call", cell="owner_call:knockout",
                     product_type="knockout", direction="short", isin=KO, underlying_isin=ALIASES["SPX"].isin,
                     underlying_label="S&P 500", status="open", liquid=True, units=10, entry_price=Decimal("1"),
                     entry_fee=Decimal("0.99"), stop_price=Decimal("0.5"), last_bid=Decimal("1"),
                     barrier_or_strike=Decimal("6300"), underlying_entry=Decimal("600"), entry_at=at.astimezone(UTC),
                     last_quote_at=at.astimezone(UTC), max_exit_at=(at + timedelta(hours=5)).astimezone(UTC))
    db.add(p)
    db.flush()
    ref = service.barrier_in_reference("short", Decimal("600"), "0.05")
    assert ref == Decimal("630")  # SPY units, not 6,300 index points
    append_event(db, "fill", service.MODE, {"barrier_ref": str(ref)}, p.id, now=at)
    t = at + timedelta(minutes=30)
    p.last_quote_at = (t - timedelta(minutes=1)).astimezone(UTC)
    db.commit()
    bar = {"t": (at + timedelta(minutes=10)).astimezone(UTC), "l": Decimal("605"), "h": Decimal("620")}
    monkeypatch.setattr(news, "alpaca_for", lambda db_: _FakeAlpaca(Decimal("610"), t - timedelta(seconds=5), [bar]))
    no_quote = FakeSc(standard_handler({}, now=t))
    assert service.manage_open_positions(db, no_quote.cli(), t, {}) == [] and p.status == "open"
    spike = {**bar, "h": Decimal("631")}
    monkeypatch.setattr(news, "alpaca_for", lambda db_: _FakeAlpaca(Decimal("610"), t - timedelta(seconds=5), [spike]))
    closed = service.manage_open_positions(db, no_quote.cli(), t, {})
    assert [r for _, r in closed] == ["knock_out"] and p.underlying_move is not None


# -- 2. freshness ------------------------------------------------------------------------------


def test_a_stale_or_outdated_reference_price_is_not_used(monkeypatch):
    t = NOW + timedelta(minutes=5)
    old = FakeSc(standard_handler({DAX_ETF: ("100", "100.1")}, now=NOW))  # stamped 5 minutes ago
    assert service.underlying_price(None, old.cli(), ALIASES["DAX"], now=t) is None

    def outdated(args):
        return {"result": quote_result(DAX_ETF, 100, 100.1, now=t, outdated=True)}

    assert service.underlying_price(None, FakeSc(outdated).cli(), ALIASES["DAX"], now=t) is None
    monkeypatch.setattr(news, "alpaca_for", lambda db_: _FakeAlpaca(Decimal("600"), t - timedelta(minutes=3), []))
    assert service.underlying_price(None, None, ALIASES["SPX"], now=t) is None
    monkeypatch.setattr(news, "alpaca_for", lambda db_: _FakeAlpaca(Decimal("600"), t - timedelta(seconds=3), []))
    assert service.underlying_price(None, None, ALIASES["SPX"], now=t) == Decimal("600")


def test_a_frozen_proxy_quote_neither_stops_a_cfd_nor_resets_its_quote_lost_clock():
    db = _memory_db()
    user = make_user(db)
    sc = FakeSc(standard_handler({DAX_ETF: ("100", "100")}, chart_pts=daily_points(), now=NOW))
    p = cfd.open_signal(db, user.id, "DAX", ALIASES["DAX"], "long", NOW + timedelta(minutes=27), sc.cli(), NOW)
    db.commit()

    def frozen_at(t):
        return FakeSc(lambda args: {"result": quote_result(DAX_ETF, 50, 50, now=t, outdated=True)}).cli()

    t = NOW + timedelta(minutes=5)
    assert cfd.manage(db, frozen_at(t), t, {}) == [] and service._aware(p.last_quote_at) == NOW.astimezone(UTC)
    # The owner's close needs a fresh price too (no fall back to the last stored one).
    assert cfd.owner_close(db, p, frozen_at(t), t) is False and p.status == "open"
    t2 = NOW + timedelta(minutes=10, seconds=30)
    assert [r for _, r in cfd.manage(db, frozen_at(t2), t2, {})] == ["quote_lost"]
    assert p.exit_price == Decimal("100")  # the last fresh price, not the frozen 50


def test_an_overnight_position_is_not_quote_lost_at_the_mornings_first_tick():
    last_night = datetime(2026, 11, 17, 21, 44, tzinfo=cal.BERLIN).astimezone(UTC)
    pos = SimpleNamespace(stop_price=None, max_exit_at=None, last_quote_at=last_night, entry_at=last_night)
    assert check_exit(pos, None, datetime(2026, 11, 18, 8, 6, tzinfo=cal.BERLIN)) is None
    assert check_exit(pos, None, datetime(2026, 11, 18, 8, 14, tzinfo=cal.BERLIN)) is None
    assert check_exit(pos, None, datetime(2026, 11, 18, 8, 15, tzinfo=cal.BERLIN)) == "quote_lost"


def test_an_ideas_quote_age_and_tap_window_are_measured_after_the_market_reads():
    db = _memory_db()
    user = make_user(db)
    sc = _handler({KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")}, NOW,
                  {"knockout": [_ko_listing(19000, 0.25, ALIASES["DAX"].isin)]})
    # The reads took 70 s: the quote stamped 10 s before the start is 80 s old when it arrives.
    slow = service.propose(db, user.id, signal="owner_call", underlying=ALIASES["DAX"], direction="long",
                           product_type="knockout", cli=sc.cli(), now=NOW, clock=lambda: NOW + timedelta(seconds=70))
    assert slow.position is None and slow.reason == "no_candidate"
    # 30 s of reads: still fresh, and the 180 s window starts when the proposal is ready.
    ready = NOW + timedelta(seconds=30)
    res = service.propose(db, user.id, signal="owner_call", underlying=ALIASES["DAX"], direction="long",
                          product_type="knockout", cli=sc.cli(), now=NOW, clock=lambda: ready)
    proposal = json.loads(db.query(DeskEvent).filter(DeskEvent.kind == "proposal").all()[-1].payload)
    assert res.nonce and datetime.fromisoformat(proposal["proposed_at"]) == ready


# -- 3. errors and messages tied to their own piece of work ------------------------------------


class _BusyEs50(FakeSc):
    """sc refuses the ES50 proxy's chart (the sync holds the session lock)."""

    def __call__(self, argv, **kw):
        args = [a for a in argv[1:] if a != "--json"]
        if args[:2] == ["broker", "chart"] and "IE0008471009" in args:
            out = {"ok": False, "command": "q", "error": {"code": "session_busy", "message": "busy"}, "hints": []}
            return subprocess.CompletedProcess(argv, 20, json.dumps(out).encode(), b"")
        return super().__call__(argv, **kw)


def test_one_index_failing_a_read_neither_rolls_back_the_other_nor_announces_it_twice():
    db = _memory_db()
    user = make_user(db)
    enter = index_momentum.plans_for_day(datetime(2026, 11, 18).date())[0].enter_at  # DAX and ES50, 17:00 Berlin
    day_pts = [{"mid_price": 100, "timestamp_utc": "2026-11-17T16:30:00Z"},
               {"mid_price": 101, "timestamp_utc": "2026-11-18T08:30:00Z"}]
    wi = {"isin": "DE000WA000A1", "strategy": "call", "issuer": "X", "omega": 8, "delta": 0.5,
          "implied_volatility": 0.2, "underlying_isin": ALIASES["DAX"].isin,
          "expiry_date": {"epoch_day": (datetime(2027, 1, 31).date() - datetime(1970, 1, 1).date()).days}}
    at = enter + timedelta(seconds=5)
    std = standard_handler({KO: ("0.995", "1.00"), "DE000WA000A1": ("0.995", "1.00"), DAX_ETF: ("100", "100.1")},
                           {"knockout": [ko_item(KO, lev=4, distance=0.25)], "warrant": [wi]}, now=at)

    def handler(args):
        if args[:2] == ["broker", "chart"] and args[args.index("--timeframe") + 1] == "7d":
            return {"data_points": day_pts}
        return std(args)

    opened: list[str] = []
    errors: list[str] = []
    fired = index_momentum.fire_due_signals(db, user.id, _BusyEs50(handler).cli(), at, opened, errors)
    db.commit()
    assert fired == ["DAX:2026-11-18"] and errors == ["session_busy"]
    assert {db.get(DeskPosition, i).product_type for i in opened} == {"knockout", "warrant", "cfd"}
    keys = [json.loads(e.payload)["key"] for e in db.query(DeskEvent).filter(DeskEvent.kind == "signal")]
    assert keys == ["DAX:2026-11-18"]  # ES50 is retried next minute, not logged yet
    msgs = jobs._announce_fills(db, at, opened)
    assert len(msgs) == 1 and "Also on paper (no action):" in msgs[0]  # one message per index (commit 4)
    assert msgs[0].count("\n  ") == 2  # the knock-out and the warrant
    # ES50's last minute: logged as no data. Nothing new opened, so DAX's fills are not announced again.
    last = enter + index_momentum.FIRE_WINDOW - timedelta(seconds=30)
    opened2: list[str] = []
    assert index_momentum.fire_due_signals(db, user.id, _BusyEs50(handler).cli(), last, opened2) == []
    signal = json.loads(db.query(DeskEvent).filter(DeskEvent.kind == "signal").all()[-1].payload)
    assert signal == {"key": "ES50:2026-11-18", "fired": False, "reason": "no_data", "error": "session_busy"}
    assert opened2 == [] and jobs._announce_fills(db, last, opened2) == []


def test_the_news_stream_and_the_monitor_do_not_overwrite_each_others_health():
    db = _memory_db()
    service.set_health(db, wrapper_outdated=True)
    service.set_stream_health(db, "connected", NOW)
    service.set_health(db, last_tick_at="t")
    service.set_stream_health(db, "closed", NOW)
    h = service.health(db)
    assert h["wrapper_outdated"] is True and h["last_tick_at"] == "t" and h["news_stream"] == "closed"


def test_a_tap_without_a_scalable_session_says_so(monkeypatch):
    db = _memory_db()
    user = make_user(db)

    def no_session(db_):
        raise ScalableError("not_logged_in", "no session")

    monkeypatch.setattr("app.foundation.scalable.service.make_cli", no_session)
    out = telegram.handle_take(db, SimpleNamespace(user_id=user.id), "1", "desk:take:x:n")
    assert out == "Market read failed: not_logged_in"


def test_desk_status_hashes_only_new_events(monkeypatch):
    db = _memory_db()
    monkeypatch.setattr(desk_chain, "_checkpoint", {"seq": 0, "hash": desk_chain.GENESIS_HASH,
                                                    "full_at": float("-inf")})
    for i in range(5):
        append_event(db, "error", service.MODE, {"code": f"e{i}"}, now=NOW + timedelta(seconds=i))
    db.commit()
    calls = []
    real = desk_chain.compute_hash
    monkeypatch.setattr(desk_chain, "compute_hash", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert desk_chain.verify_chain_cached(db) == (True, None) and len(calls) == 5
    append_event(db, "error", service.MODE, {"code": "e5"}, now=NOW + timedelta(seconds=9))
    db.commit()
    calls.clear()
    assert desk_chain.verify_chain_cached(db) == (True, None) and len(calls) == 2  # the checkpoint row + 1 new
    row = db.query(DeskEvent).filter(DeskEvent.seq == 6).one()
    row.payload = "{}"
    db.commit()
    assert desk_chain.verify_chain_cached(db) == (False, 6)
    assert api_ops.chain_status(db)["ok"] is False  # the explicit endpoint always re-hashes everything


# -- 4. silent wrong answers -------------------------------------------------------------------


def test_2d_is_refused_with_the_reason():
    with pytest.raises(owner_calls.IdeaParseError, match="at most 1d"):
        owner_calls.parse_idea("/idea SAP up 2d")
    assert owner_calls.HORIZON_DAYS == {"today": 1, "1d": 2}


def test_close_takes_a_literal_id_of_the_owners_own_position(monkeypatch):
    db = _memory_db()
    owner = make_user(db)
    other = make_user(db, role="admin", name="other")
    said: list[str] = []
    monkeypatch.setattr(telegram, "_say", lambda db_, chat, text, markup=None: said.append(text))
    pos = DeskPosition(user_id=other.id, cell="owner_call:knockout", signal="owner_call", product_type="knockout",
                       isin=KO, underlying_isin=ALIASES["DAX"].isin, direction="long", status="open", units=1,
                       entry_price=Decimal(1), entry_at=NOW)
    db.add(pos)
    db.commit()
    for text in ("/close %", "/close _", "/close a"):
        telegram.handle_close(db, SimpleNamespace(user_id=owner.id), "1", text)
        assert said[-1].startswith("Usage")
    telegram.handle_close(db, SimpleNamespace(user_id=owner.id), "1", f"/close {pos.id[:8]}")
    assert said[-1] == "No single open position with that id." and pos.status == "open"


ATOM = b"""<?xml version="1.0" encoding="ISO-8859-1" ?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>8-K - NVIDIA CORP (0001045810) (Filer)</title><link rel="alternate" type="text/html" href="https://sec.example/1"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-20 &lt;br&gt;Item 4.02: Non-Reliance &lt;br&gt;Item 9.01: Exhibits</summary>
<updated>2026-11-20T13:00:13-05:00</updated><id>urn:tag:sec.gov,2008:accession-number=0001-26-1</id></entry></feed>"""
OTHER = ATOM.replace(b"NVIDIA CORP (0001045810)", b"OTHER INC (0009999999)").replace(b"0001-26-1", b"0009-26-1")


def test_a_holdings_first_8k_is_sent_not_stored_as_the_baseline(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    sent: list[str] = []
    monkeypatch.setattr("app.decision.desk.telegram.alert", lambda db_, uid, text: sent.append(text))
    monkeypatch.setattr(newswire, "watchlist", lambda db_, uid: {"NVDA": "holding:NVDA"})
    monkeypatch.setattr(newswire, "holding_ciks", lambda db_, watch: {"0001045810": "NVDA"})

    async def rated(db_, item):
        return newswire.Rating(3, "down", "company", 1)

    monkeypatch.setattr(newswire, "rate", rated)
    feed = {"body": OTHER}

    def fetch(url, ua):
        if "sec.gov" in url:
            return feed["body"]
        raise ValueError("HTTP 503")

    weeks_ago = datetime(2026, 10, 30, 19, 1, tzinfo=UTC)
    assert newswire.poll_feeds(db, user.id, weeks_ago, fetch=fetch) == {}  # nothing of the owner's: no row stored
    assert db.query(NewsWireItem).count() == 0 and "sec_8k" in newswire._sources_read(db)
    feed["body"] = ATOM  # weeks later: NVIDIA's first 8-K
    now = datetime(2026, 11, 20, 18, 1, tzinfo=UTC)
    assert newswire.poll_feeds(db, user.id, now, fetch=fetch) == {"sec_8k": 1}
    assert db.query(NewsWireItem).one().alert == "sent" and len(sent) == 1


def test_an_idea_by_isin_gets_its_ticker_for_the_earnings_guard(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    monkeypatch.setattr("app.foundation.live_positions.combined_positions",
                        lambda db_, uid: [SimpleNamespace(isin="US0378331005", ticker="AAPL", name="Apple")])
    u = resolve_underlying(db, "US0378331005", user.id)
    assert u.symbol == "AAPL"
    asked: list[str | None] = []
    monkeypatch.setattr(news, "earnings_in_window", lambda db_, symbol, *a: asked.append(symbol) or True)
    sc = _handler({KO: ("0.995", "1.00"), u.isin: ("140", "140.1")}, NOW.replace(hour=16),
                  {"knockout": [_ko_listing(150, 0.25, u.isin)]})
    res = service.propose(db, user.id, signal="owner_call", underlying=u, direction="long", product_type="knockout",
                          cli=sc.cli(), now=NOW.replace(hour=16))
    assert asked == ["AAPL"] and res.position.status == "rejected" and "event_block_earnings" in res.reason


def test_a_us_stock_with_an_unreadable_earnings_calendar_is_blocked_unless_the_owner_says_event(monkeypatch):
    db = _memory_db()
    user = make_user(db)
    monkeypatch.setattr(news, "earnings_in_window", lambda *a, **k: None)
    at = NOW.replace(hour=16)
    sc = _handler({KO: ("0.995", "1.00"), US_STOCK.isin: ("140", "140.1")}, at,
                  {"knockout": [_ko_listing(150, 0.25, US_STOCK.isin)]})
    blocked = service.propose(db, user.id, signal="owner_call", underlying=US_STOCK, direction="long",
                              product_type="knockout", cli=sc.cli(), now=at)
    assert blocked.position.status == "rejected" and blocked.reason == "earnings_unknown"
    accepted = service.propose(db, user.id, signal="owner_call", underlying=US_STOCK, direction="long",
                               product_type="knockout", cli=sc.cli(), now=at, event_flag=True)
    assert accepted.nonce and accepted.position.status == "proposed"
    proposal = json.loads(db.query(DeskEvent).filter(DeskEvent.kind == "proposal").all()[-1].payload)
    assert proposal["earnings_in_window"] == "unknown"
    # An index has no earnings: unknown does not block it.
    dax = _handler({KO: ("0.995", "1.00"), DAX_ETF: ("100", "100.1")}, at,
                   {"knockout": [_ko_listing(19000, 0.25, ALIASES["DAX"].isin)]})
    assert service.propose(db, user.id, signal="owner_call", underlying=ALIASES["DAX"], direction="long",
                           product_type="knockout", cli=dax.cli(), now=at).nonce


def test_a_refused_idea_says_why_in_words_and_how_to_proceed():
    res = service.ProposalResult(None, "earnings_unknown")
    assert "add 'event'" in owner_calls.format_proposal(res, US_STOCK, "knockout")
    assert owner_calls.reason_text("spread_too_wide,quote_stale") == "the spread is too wide; the quote is too old"
    assert owner_calls.reason_text("brand_new_code") == "brand new code"


def test_control_center_names_failing_desk_reads_and_a_long_dropped_stream():
    from app.foundation.control_center import attention_items
    from app.foundation.settings import upsert_public_settings

    db = _memory_db()
    make_user(db)

    def ids():
        return {i["id"] for i in attention_items(db)}

    assert not {"desk-read-failing", "desk-news-stream"} & ids()
    now = datetime.now(UTC)
    upsert_public_settings(db, {"desk_health_json": {"last_error": "session_expired"},
                                "desk_news_stream_json": {"news_stream": "closed",
                                                          "news_stream_at": (now - timedelta(minutes=5)).isoformat()}})
    assert "desk-read-failing" in ids() and "desk-news-stream" not in ids()  # a short drop reconnects
    upsert_public_settings(db, {"desk_news_stream_json": {"news_stream": "closed",
                                                          "news_stream_at": (now - timedelta(minutes=30)).isoformat()}})
    assert "desk-news-stream" in ids()
    upsert_public_settings(db, {"desk_news_stream_json": {"news_stream": "no_key",
                                                          "news_stream_at": (now - timedelta(hours=3)).isoformat()}})
    assert "desk-news-stream" not in ids()
