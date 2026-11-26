"""Thin US stocks on the event desk (2026-11-24, SHOE): prices from real trades, the owner's own stop, cents."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from test_desk_event_messages import _hype
from test_desk_events import _setup

from app.decision.desk import cfd_instruments as ci
from app.decision.desk import cfd_journal, event_messages, events, news, service, telegram
from app.decision.desk.underlyings import Underlying

NOW = datetime(2026, 11, 24, 15, 28, 28, tzinfo=UTC)
SHOE = Underlying("US8243121071", "Shoe Station Group", "XNYS", False, symbol="SHOE", alpaca_symbol="SHOE")


class _Alpaca:
    def __init__(self, bid, ask, trade=None, trade_age_s=30):
        self.bid, self.ask, self.trade, self.trade_age_s = Decimal(bid), Decimal(ask), trade, trade_age_s
        self.trade_reads = 0

    def get_quote(self, symbol):
        mid = (self.bid + self.ask) / 2
        return {"ok": True, "data": {"close": mid, "bid": self.bid, "ask": self.ask},
                "quality": {"as_of": NOW - timedelta(seconds=3)}}

    def latest_trade(self, symbol):
        self.trade_reads += 1
        if self.trade is None:
            return None
        return {"price": Decimal(self.trade), "as_of": NOW - timedelta(seconds=self.trade_age_s)}


def _price(monkeypatch, fake, errors=None):
    monkeypatch.setattr(news, "alpaca_for", lambda db: fake)
    return service.underlying_price(None, None, SHOE, now=NOW, errors=errors)


def test_a_narrow_quote_gives_its_midpoint_without_a_trade_read(monkeypatch):
    fake = _Alpaca("11.18", "11.22", trade="11.00")
    assert _price(monkeypatch, fake) == Decimal("11.20") and fake.trade_reads == 0


def test_a_wide_quote_gives_the_last_real_trade(monkeypatch):
    # The quote that stopped the paper short: bid 11.18, ask 12.79, midpoint 11.985; the last trade was 11.26.
    assert _price(monkeypatch, _Alpaca("11.18", "12.79", trade="11.26")) == Decimal("11.26")


def test_a_wide_quote_without_a_recent_trade_gives_no_price(monkeypatch):
    errors: list[str] = []
    assert _price(monkeypatch, _Alpaca("11.18", "12.79", trade="11.26", trade_age_s=301), errors) is None
    assert errors == ["wide_quote"]
    assert _price(monkeypatch, _Alpaca("11.18", "12.79", trade=None)) is None


def _entered_short(db, user, fill):
    p = _hype(db, user)  # NVDA short, signal 480, stop distance 30 (paper stop 510)
    j = cfd_journal.for_position(db, p.id)
    j.status, j.entry_level, j.quantity = "entered", Decimal(fill), Decimal("0.17")
    db.commit()
    return p, j


def test_a_paper_stop_does_not_tell_the_owner_to_close_when_their_own_stop_is_not_reached():
    db, user = _setup()
    p, j = _entered_short(db, user, "495")  # filled 15 higher: own stop 525
    assert events.event_close(db, p, Decimal("512"), "stop", NOW, ci.by_key("US:NVDA"))
    text = event_messages.close_text(db, p, "stop", j)
    assert "CLOSE now" not in text
    assert text.startswith("NVIDIA (NVDA): the paper trade was stopped out at 512.00. Your own stop for your fill, "
                           "525.00, is not reached, so this is not a close signal for your trade.")
    assert "planned exit (2026-12-02 at 21:40 Berlin)" in text
    assert "reply to this message with: <exit price>" in text


def test_a_trailed_stop_is_judged_against_the_owners_own_stop_like_a_stop():
    db, user = _setup()
    p, j = _entered_short(db, user, "495")  # own stop 525, not reached at 512
    assert events.event_close(db, p, Decimal("512"), "trail", NOW, ci.by_key("US:NVDA"))
    text = event_messages.close_text(db, p, "trail", j)
    assert "CLOSE now" not in text and "is not reached" in text


def test_a_paper_stop_says_close_when_the_owners_stop_is_reached_too():
    db, user = _setup()
    p, j = _entered_short(db, user, "481")  # own stop 511
    assert events.event_close(db, p, Decimal("512"), "stop", NOW, ci.by_key("US:NVDA"))
    assert event_messages.close_text(db, p, "stop", j).startswith("NVIDIA (NVDA): CLOSE now (stop-loss level reached)")


def test_a_stock_fill_is_confirmed_in_cents_and_price_words():
    j = SimpleNamespace(market_key="US:ABC", entry_level=Decimal("21.37"), index_level_est=Decimal("20.9"),
                        stop_points=Decimal("1.5"), direction="short")
    text = telegram._noted_in(j)
    assert text == ("Noted: US:ABC in at 21.37 (0.47 better than the signal's price). "
                    "Stop for your fill: 22.87 (1.50 from your fill).")
    idx = SimpleNamespace(market_key="DAX", entry_level=Decimal("24010.5"), index_level_est=Decimal("24000"),
                          stop_points=Decimal("120"), direction="long")
    assert telegram._noted_in(idx) == ("Noted: DAX in at 24,010.5 (10.5 pts worse than the signal's index level). "
                                       "Stop for your fill: 23,890.5 (120 pts).")


def test_the_amended_rules_register_beside_the_first_registration():
    # Prod holds the first registration (hash 0350c35d...); the first tick after the deploy adds the new one.
    from app.decision.desk import prereg_events
    from app.foundation.models.entities import DeskRegistration

    db, _ = _setup()
    old_at = NOW - timedelta(hours=3)
    db.query(DeskRegistration).filter(DeskRegistration.prereg_hash == prereg_events.EVENT_HASH).delete()
    db.add(DeskRegistration(prereg_hash="0350c35d98bb5e06f434c7a6e2858b2c2fc69e6942dece4978265ad7da38e579",
                            spec="{}", registered_at=old_at))
    db.commit()
    assert prereg_events.registered_at_events(db) is None
    prereg_events.register_events(db, now=NOW)
    prereg_events.register_events(db, now=NOW + timedelta(minutes=1))  # idempotent
    assert db.query(DeskRegistration).count() == 2
    assert prereg_events.registered_at_events(db) == NOW
