"""CFD message v2 (ADR 0027 amendment 2026-11-23): context at the signal, exact text, no rule change."""
from datetime import timedelta
from decimal import Decimal

from desk_helpers import NOW, make_user
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import cfd, prereg
from app.decision.desk.signals import index_momentum
from app.decision.desk.underlyings import ALIASES
from app.foundation.core.db import Base
from app.foundation.models.entities import DeskEvent, DeskPosition


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _ctx(monkeypatch, key="DAX", sigma="0.0100", ref="100", prev="100", index_prev="25000", fx="1", r1="0.0042"):
    monkeypatch.setattr(cfd, "_index_prev_close", lambda db, ticker, day: Decimal(index_prev))
    monkeypatch.setattr(cfd, "_eur_per_usd", lambda db, now: Decimal(fx))
    enter = NOW.replace(hour=17, minute=0)
    return cfd.prepare_context(None, key, ALIASES[key], Decimal(r1), Decimal(prev), Decimal(ref), Decimal(sigma),
                               enter + timedelta(minutes=5), enter + timedelta(minutes=27), NOW)


def _position(key="DAX", direction="long"):
    u = ALIASES[key]
    exit_at = NOW.replace(hour=17, minute=27)
    return DeskPosition(user_id="u", cell=cfd.CELL, signal="index_momentum", product_type="cfd", isin=u.isin,
                        underlying_isin=u.isin, underlying_label=u.label, direction=direction, quantity=Decimal(1),
                        entry_price=Decimal(100), max_exit_at=exit_at, stop_price=Decimal(99),
                        product_name=f"{cfd.MARKETS[key][0]} CFD (Trading 212, manual)")


def test_prereg_hash_is_unchanged():
    assert prereg.PREREG_HASH == "86396b951089aa96007d6227bbfda59991be23b7b8b8ecd2975af6afa5c4a8cd"


def test_context_values_and_quantity_rounds_down(monkeypatch):
    ctx = _ctx(monkeypatch)
    assert ctx.index_level_est == Decimal("25000") and ctx.exposure_eur == Decimal("500.00")
    # 500 / 25000 = 0.02 -> 0.0200; stop = 1 % of the level; margin 5 %
    assert ctx.quantity_t212 == Decimal("0.0200") and ctx.stop_points == Decimal("250")
    assert ctx.margin_eur == Decimal("25") and ctx.spread_points == Decimal("1.25")
    odd = _ctx(monkeypatch, index_prev="25432.1")
    assert odd.quantity_t212 == Decimal("0.0196")  # 500 / 25432.1 = 0.019660 -> down, not 0.0197


def test_dax_long_message_text(monkeypatch):
    ctx = _ctx(monkeypatch)
    text = cfd.open_message(None, _position(), "DAX", NOW, ctx)
    assert text.splitlines() == [
        "DAX long: CFD, manual (Trading 212: Germany 40 (GER40))",
        "Why: DAX +0.42% from yesterday's close to 09:30 Berlin (rule: the last 30 minutes follow it)",
        "Quantity 0.0200 (≈ 500 EUR, margin ≈ 25.00 EUR). Enter by 17:05 Berlin, otherwise skip today.",
        "Stop: 250 pts from your fill (1 daily sigma; a 27-minute move this large is rare)",
        "Typical last-30-minute move ±61 pts; spread ≈ 1.2 pts (Trading 212, observed)",
        "Close at 17:27 Berlin; I will tell you.",
        "Paper-scored as index_momentum:cfd. Most retail CFD accounts lose money; nothing is ordered for you.",
    ]


def test_spx_short_message_has_the_usd_line_and_new_york_reason(monkeypatch):
    ctx = _ctx(monkeypatch, key="SPX", fx="0.9", index_prev="6800", r1="-0.0031")
    text = cfd.open_message(None, _position("SPX", "short"), "SPX", NOW, ctx)
    lines = text.splitlines()
    assert lines[0] == "SPX short: CFD, manual (Trading 212: USA 500 (USA500))"
    assert "-0.31% from yesterday's close to 10:00 New York (16:00 Berlin)" in lines[1]
    assert "USD market: 0.5 % FX fee on the result at close." in lines
    assert ctx.eur_per_unit == Decimal("0.9") and ctx.quantity_t212 == Decimal("0.0816")  # 500/(6800*0.9)


def test_without_context_the_short_text_with_the_reason_fix(monkeypatch):
    monkeypatch.setattr(cfd, "_index_level", lambda db, t: Decimal("24000"))
    text = cfd.open_message(_memory_db(), _position(direction="short"), "DAX", NOW, None)
    assert text.startswith("CFD signal (manual, Trading 212): SELL Germany 40 (GER40) now")
    assert "first half hour" not in text and "went down from yesterday's close to 09:30 Berlin" in text


def test_missing_inputs_give_no_context(monkeypatch):
    monkeypatch.setattr(cfd, "_index_prev_close", lambda db, t, d: None)
    monkeypatch.setattr(cfd, "_eur_per_usd", lambda db, n: Decimal(1))
    assert cfd.prepare_context(None, "DAX", ALIASES["DAX"], Decimal("0.01"), Decimal(100), Decimal(100),
                               Decimal("0.01"), NOW, NOW, NOW) is None


def test_index_close_of_today_is_ignored(monkeypatch):
    rows = [{"date": "2026-11-17", "close": 24900.0}, {"date": "2026-11-18", "close": 25100.0}]
    monkeypatch.setattr("app.foundation.market.history", lambda db, t, days=10: rows)
    assert cfd._index_prev_close(None, "^GDAXI", NOW.date()) == Decimal("24900.0")


def test_paper_position_is_identical_with_and_without_context(monkeypatch):
    out = []
    for with_ctx in (False, True):
        db = _memory_db()
        user = make_user(db)
        ctx = _ctx(monkeypatch) if with_ctx else None
        p = cfd.record_signal(db, user.id, "DAX", ALIASES["DAX"], "long", NOW + timedelta(minutes=27), NOW,
                              Decimal(100), Decimal("0.01"), context=ctx)
        out.append({c: getattr(p, c) for c in ("quantity", "entry_price", "stop_price", "spread_pct", "cell")})
        if with_ctx:
            import json
            fill = json.loads(db.query(DeskEvent).filter(DeskEvent.kind == "fill").one().payload)
            assert fill["quantity_t212"] == "0.0200" and fill["r1"] == "0.0042"
    assert out[0] == out[1]


def test_r1_parts_match_r1_and_return_the_previous_close():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    berlin = ZoneInfo("Europe/Berlin")
    day = datetime(2026, 11, 18).date()
    pts = [(datetime(2026, 11, 17, 17, 30, tzinfo=berlin), Decimal(100)),
           (datetime(2026, 11, 18, 9, 30, tzinfo=berlin), Decimal(101))]
    assert index_momentum.compute_r1_xetra_parts(pts, day) == (Decimal("0.01"), Decimal(100))
    assert index_momentum.compute_r1_xetra(pts, day) == Decimal("0.01")
