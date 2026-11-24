from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.decision.desk import cfd_instruments as ci
from app.decision.desk import event_sources as es
from app.foundation import technical
from app.foundation.core.db import Base
from app.foundation.models.entities import NewsWireItem
from app.foundation.providers.alpaca_provider import DataUnavailable

NOW = datetime(2026, 11, 24, 16, 0, tzinfo=UTC)  # 11:00 New York, a Tuesday


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class FakeProvider:
    def __init__(self, change=1.12, vol=5000.0, news_today=6, headlines=None, screener=True):
        self.change, self.vol, self.news_today, self.screener = change, vol, news_today, screener
        self.headlines = headlines or ["moves on chatter"] * news_today

    def screener_movers(self, top=50):
        if not self.screener:
            raise DataUnavailable("screener_not_entitled")
        return [{"symbol": "PLTR", "percent_change": 12.0, "price": 112.0}]

    def screener_most_actives(self, top=100):
        return []

    def get_intraday_bars(self, symbol, tf, start, end):
        rows = []
        for k in range(40, 0, -1):
            d = NOW - timedelta(days=k)
            if d.weekday() > 4:
                continue
            rows.append({"t": d.replace(hour=14), "o": 100, "h": 102, "l": 98, "c": 100, "v": 1000})
        rows.append({"t": NOW.replace(hour=14, minute=30), "o": 100, "h": 113, "l": 100, "c": 100 * self.change,
                     "v": self.vol})
        return rows

    def get_news(self, symbols, start, limit=20):
        recent = (NOW - start) < timedelta(days=2)
        n = self.news_today if recent else self.news_today + 20
        heads = self.headlines if recent else ["x"] * n
        return [{"id": str(i), "headline": heads[i % len(heads)], "created_at": "", "symbols": symbols}
                for i in range(n)]


def test_window():
    assert es.in_hype_window(NOW)
    assert not es.in_hype_window(datetime(2026, 11, 24, 15, 0, tzinfo=UTC))  # 10:00 New York
    assert not es.in_hype_window(datetime(2026, 11, 24, 21, 0, tzinfo=UTC))  # 16:00 New York
    assert not es.in_hype_window(datetime(2026, 11, 28, 16, 0, tzinfo=UTC))  # Saturday


def test_hype_candidate():
    db = _memory_db()
    out = es.prepare_hype(db, FakeProvider(), NOW)
    assert len(out) == 1
    c = out[0]
    assert c.cell == "hype_fade:cfd" and c.direction == "short" and c.inst.key == "US:PLTR"
    assert c.stop_distance > 0 and c.max_exit_at > NOW and c.evidence["volume_ratio"] == 5.0
    assert "hype fade" in c.reason


def test_hype_needs_everything():
    db = _memory_db()
    assert es.prepare_hype(db, FakeProvider(change=1.05), NOW) == []  # +5 % only
    assert es.prepare_hype(db, FakeProvider(vol=2000.0), NOW) == []  # 2x volume
    assert es.prepare_hype(db, FakeProvider(news_today=3), NOW) == []  # too little news
    assert es.prepare_hype(db, None, NOW) == []
    assert es.prepare_hype(db, FakeProvider(), datetime(2026, 11, 24, 15, 0, tzinfo=UTC)) == []


def test_earnings_filter_separates_hype_from_news():
    db = _memory_db()
    prov = FakeProvider(headlines=["PLTR posts record Q3 results"])
    assert es.prepare_hype(db, prov, NOW) == []
    db2 = _memory_db()
    db2.add(NewsWireItem(id="sec_8k:1", source="sec_8k", headline="8-K", symbols="PLTR", received_at=NOW))
    db2.commit()
    assert es.prepare_hype(db2, FakeProvider(), NOW) == []


def test_screener_403_falls_back_to_wire():
    db = _memory_db()
    db.add(NewsWireItem(id="alpaca:1", source="alpaca", headline="PLTR jumps", symbols="PLTR",
                        received_at=NOW - timedelta(hours=1), materiality=2))
    db.commit()
    errors: list[str] = []
    out = es.prepare_hype(db, FakeProvider(screener=False), NOW, errors=errors)
    assert errors == ["screener_unavailable"] and [c.inst.key for c in out] == ["US:PLTR"]


def _bars():
    out = []
    for k in range(60, 0, -1):
        d = date(2026, 11, 24) - timedelta(days=k)
        c = 100 + (0.5 if k % 2 else -0.5)
        out.append(technical.Bar(d, c, c + 1, c - 1, c))
    return out


def test_news_drift(monkeypatch):
    db = _memory_db()
    db.add(NewsWireItem(id="sec_8k:2", source="sec_8k", headline="8-K: results", symbols="NVDA",
                        received_at=NOW - timedelta(minutes=10), materiality=3, scope="company", direction="up"))
    db.commit()
    monkeypatch.setattr(ci, "daily_bars", lambda db, inst, days=400: _bars())
    monkeypatch.setattr(ci, "reference_price", lambda *a, **k: Decimal("103"))
    out = es.prepare_news(db, None, NOW)
    assert len(out) == 1 and out[0].direction == "long" and out[0].cell == "news_drift:cfd"
    monkeypatch.setattr(ci, "reference_price", lambda *a, **k: Decimal("100.1"))
    assert es.prepare_news(db, None, NOW) == []  # no move yet
    monkeypatch.setattr(ci, "reference_price", lambda *a, **k: Decimal("97"))
    assert es.prepare_news(db, None, NOW) == []  # moved against the reading


def test_news_needs_primary_source_and_rating(monkeypatch):
    db = _memory_db()
    db.add(NewsWireItem(id="alpaca:9", source="alpaca", headline="analyst likes NVDA", symbols="NVDA",
                        received_at=NOW - timedelta(minutes=5), materiality=3, scope="company", direction="up"))
    db.add(NewsWireItem(id="sec_8k:3", source="sec_8k", headline="8-K", symbols="NVDA",
                        received_at=NOW - timedelta(minutes=5), materiality=2, scope="company", direction="up"))
    db.commit()
    monkeypatch.setattr(ci, "daily_bars", lambda db, inst, days=400: _bars())
    monkeypatch.setattr(ci, "reference_price", lambda *a, **k: Decimal("103"))
    assert es.prepare_news(db, None, NOW) == []


def test_hype_screener_prefilter_skips_small_movers_before_any_bar_read():
    db = _memory_db()

    class Counting(FakeProvider):
        def __init__(self, movers, **kw):
            super().__init__(**kw)
            self.movers, self.bar_reads = movers, []

        def screener_movers(self, top=50):
            return self.movers

        def get_intraday_bars(self, symbol, tf, start, end):
            self.bar_reads.append(symbol)
            return super().get_intraday_bars(symbol, tf, start, end)

    p = Counting([{"symbol": "PLTR", "percent_change": 12.0, "price": 112.0},
                  {"symbol": "SMAL", "percent_change": 3.0, "price": 20.0},
                  {"symbol": "NEAR", "percent_change": 6.5, "price": 20.0}])
    out = es.prepare_hype(db, p, NOW)
    assert [c.inst.key for c in out] == ["US:PLTR", "US:NEAR"]  # NEAR passes on its bars (+12 % in the fake)
    assert p.bar_reads == ["PLTR", "NEAR"]  # +3 % never costs a request; +6.5 % is within the slack
    assert es._may_qualify({"symbol": "X", "percent_change": None})  # most-actives rows go to the full check


def test_no_news_entry_in_the_first_fifteen_minutes_of_the_session(monkeypatch):
    # 2026-11-24 SHOE: the short was entered on the opening print (10.45); three minutes later it traded near 11.
    db = _memory_db()
    open_ny = datetime(2026, 11, 24, 14, 30, tzinfo=UTC)  # 09:30 New York
    db.add(NewsWireItem(id="sec_8k:7", source="sec_8k", headline="8-K: results", symbols="NVDA",
                        received_at=open_ny - timedelta(minutes=5), materiality=3, scope="company", direction="up"))
    db.commit()
    monkeypatch.setattr(ci, "daily_bars", lambda db, inst, days=400: _bars())
    monkeypatch.setattr(ci, "reference_price", lambda *a, **k: Decimal("103"))
    assert technical.EVENT_RULES["news_drift"]["open_delay_minutes"] == 15
    assert es.prepare_news(db, None, open_ny + timedelta(seconds=13)) == []
    assert es.prepare_news(db, None, open_ny + timedelta(minutes=14, seconds=59)) == []
    out = es.prepare_news(db, None, open_ny + timedelta(minutes=15))
    assert len(out) == 1 and out[0].direction == "long"
