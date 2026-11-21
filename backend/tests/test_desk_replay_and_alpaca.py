"""The desk's Alpaca additions (intraday bars, news) and the informational replay (ADR 0027)."""
import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.foundation.providers.alpaca_provider import AlpacaProvider


def _provider(monkeypatch, pages):
    p = AlpacaProvider.__new__(AlpacaProvider)
    p.enabled, p.api_key, p.api_secret = True, "k", "s"
    calls = []

    def _get(path, params=None, base=None):
        calls.append((path, dict(params or {}), base))
        return pages[len(calls) - 1]

    monkeypatch.setattr(p, "_get", _get)
    return p, calls


def test_intraday_bars_follow_pages_and_sort(monkeypatch):
    pages = [{"bars": [{"t": "2026-11-18T15:00:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 1}], "next_page_token": "x"},
             {"bars": [{"t": "2026-11-18T14:30:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}]
    p, calls = _provider(monkeypatch, pages)
    rows = p.get_intraday_bars("SPY", "30Min", datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 11, 19, tzinfo=UTC))
    assert [r["c"] for r in rows] == [Decimal(1), Decimal(2)]
    assert calls[0][1]["timeframe"] == "30Min" and calls[0][1]["feed"] == "iex" and calls[1][1]["page_token"] == "x"


def test_news_uses_the_news_endpoint_and_skips_non_us(monkeypatch):
    p, calls = _provider(monkeypatch, [{"news": [{"id": 1, "headline": "Hello", "created_at": "t", "symbols": ["SPY"]},
                                                 {"id": 2}]}])
    got = p.get_news(["SPY", "SAP.DE"], datetime(2026, 11, 18, tzinfo=UTC))
    assert got == [{"id": "1", "headline": "Hello", "created_at": "t", "symbols": ["SPY"]}]
    assert calls[0][2] == "https://data.alpaca.markets/v1beta1/news" and calls[0][1]["symbols"] == "SPY"
    assert p.get_news(["SAP.DE"], datetime(2026, 11, 18, tzinfo=UTC)) == []


def test_replay_rule_on_synthetic_bars():
    spec = importlib.util.spec_from_file_location(
        "desk_replay", Path(__file__).resolve().parents[1] / "scripts" / "desk_index_momentum_replay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ny = mod.NY
    bars = []
    day = datetime(2026, 9, 1, tzinfo=ny)
    px = Decimal(100)
    n_days = 0
    while n_days < 40:
        day += timedelta(days=1)
        if day.weekday() > 4:
            continue
        n_days += 1
        for i in range(13):  # 09:30 .. 15:30 half hours
            t = (day.replace(hour=9, minute=30) + timedelta(minutes=30 * i)).astimezone(UTC)
            o = px
            px = px * (Decimal("1.004") if i == 0 else (Decimal("1.002") if i == 12 else Decimal(1)))
            bars.append({"t": t, "o": o, "c": px})
        px = px * Decimal("0.999") if n_days % 2 else px * Decimal("1.001")
    rows = mod.replay(bars)
    assert rows and all(r["direction"] == 1 for r in rows)  # first half hour always up
    stats = mod.summarise(rows)
    assert sum(s["n"] for s in stats.values()) == len(rows)
