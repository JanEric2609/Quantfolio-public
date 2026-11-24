from datetime import UTC, date, datetime
from decimal import Decimal

from app.decision.desk import cfd_instruments as ci
from app.decision.desk import prereg
from app.foundation import technical


def test_universe_sizes_and_facts():
    assert len(ci.INDICES) == 4 and len(ci.STOCKS) == 20
    for i in ci.UNIVERSE:
        assert i.margin_rate > 0 and i.spread_bp > 0 and i.quantity_step > 0
    spx = ci.by_key("SPX")
    assert spx.spread_bp == Decimal(prereg.SPEC["cfd"]["spread_bp"]["SPX"])
    nvda = ci.by_key("US:NVDA")
    assert nvda.spread_bp == Decimal("7.5") and not nvda.spread_assumed and nvda.usd
    assert ci.by_key("DE:SIE").spread_assumed
    assert nvda.overnight_rate("long") > nvda.overnight_rate("short")


def test_for_symbol():
    assert ci.for_symbol(None, "nvda").key == "US:NVDA"
    assert ci.for_symbol(None, "SAP.DE").key == "DE:SAP"
    assert ci.for_symbol(None, "DE0007164600").key == "DE:SAP"
    assert ci.for_symbol(None, "SPX").key == "SPX"
    adhoc = ci.for_symbol(None, "PLTR")
    assert adhoc.currency == "USD" and adhoc.spread_assumed
    assert ci.for_symbol(None, "XYZ.DE").currency == "EUR"
    assert ci.for_symbol(None, "??") is None
    assert ci.for_symbol(None, "") is None
    assert ci.for_symbol(None, "FR0000120271") is None


def test_reference_price_delegates(monkeypatch):
    seen = {}

    def fake(db, cli, u, *, now, errors=None):
        seen["u"] = u
        return Decimal("12.5")

    monkeypatch.setattr(ci.service, "underlying_price", fake)
    inst = ci.by_key("US:NVDA")
    assert ci.reference_price(None, None, inst, datetime.now(UTC)) == Decimal("12.5")
    assert seen["u"].alpaca_symbol == "NVDA"
    monkeypatch.setattr(ci.service, "underlying_price", lambda *a, **k: None)
    assert ci.reference_price(None, None, inst, datetime.now(UTC)) is None


def test_atr14_synthetic(monkeypatch):
    rows = [{"date": f"2026-01-{d:02d}", "open": 10, "high": 12, "low": 10, "close": 11} for d in range(1, 21)]
    import app.foundation.market as market

    monkeypatch.setattr(market, "history", lambda db, t, days=365, allow_live=True: rows)
    assert ci.atr14(None, ci.by_key("US:NVDA")) == Decimal("2")
    assert ci.atr14(None, ci.by_key("US:NVDA"), date(2026, 1, 5)) is None  # too few bars before the day


def test_index_expiry_and_assumed():
    assert ci.index_expiry("DAX", date(2026, 11, 24)).hour == 13
    # Third Friday of Dec 2026 is the 18th: assumed expiry is Thu the 17th at 13:00.
    assert ci.assumed_expiry(date(2026, 12, 1)).date() == date(2026, 12, 17)
    assert ci.assumed_expiry(date(2026, 12, 18)).date() == date(2027, 3, 18)
    assert ci.index_expiry("SPX", date(2026, 12, 20)).date() == date(2027, 3, 18)


def test_technical_basics():
    closes = [float(x) for x in range(1, 60)]
    assert technical.sma(closes, 5) == 57.0
    assert technical.rsi(closes, 2) == 100.0
    bars = [technical.Bar(date(2026, 1, 1), c, c + 1, c - 1, c) for c in closes]
    assert technical.atr(bars, 14) is not None
