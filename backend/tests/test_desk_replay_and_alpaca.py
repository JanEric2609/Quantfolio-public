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


# --- exit variants on 1-minute bars ------------------------------------------------------------------------

def _mday(path, r1=0.002, sigma=0.01, entry=100.0):
    from datetime import date as _date

    from app.lab.desk_replay import MinuteDay
    return MinuteDay(_date(2026, 11, 24), r1, entry, tuple(path), sigma, False)


def test_fixed_exit_is_the_last_close_and_take_profit_fills_at_its_level():
    import math

    from app.lab.desk_replay import exit_return
    unit = 0.01 * math.sqrt(27 / 390)  # ~0.263 %
    up_then_down = [(100.2, 99.9, 100.1), (100.3, 100.0, 100.2)] + [(100.0, 99.7, 99.8)] * 25
    day = _mday(up_then_down)
    assert abs(exit_return(day, "fixed") - (-0.002)) < 1e-12
    assert abs(exit_return(day, "tp_0.5") - 0.5 * unit) < 1e-12  # the first +0.2 % high passes 0.13 %
    assert abs(exit_return(day, "tp_1.0") - unit) < 1e-12  # the second bar's +0.3 % high passes 0.263 %
    assert abs(exit_return(day, "trail") - (-0.002)) < 1e-12  # armed at +0.2 %; the next close is already -0.2 %


def test_short_side_trailing_exit_and_stop():
    from app.lab.desk_replay import exit_return
    # short (r1 < 0): price falls 0.4 %, then gives back to -0.15 % -> trailing exit there
    path = [(100.0, 99.7, 99.7), (99.7, 99.6, 99.6), (99.9, 99.8, 99.85)] + [(100.5, 100.0, 100.4)] * 24
    day = _mday(path, r1=-0.002)
    assert abs(exit_return(day, "trail") - 0.0015) < 1e-9
    assert abs(exit_return(day, "fixed") - (-0.004)) < 1e-9
    stopped = _mday([(101.5, 100.0, 101.2)] + [(100.0, 99.0, 99.0)] * 26, r1=-0.002)
    assert exit_return(stopped, "fixed") == -0.01  # one daily sigma against a short


def test_no_trade_without_r1_or_sigma():
    from app.lab.desk_replay import exit_return
    assert exit_return(_mday([(1, 1, 1)] * 27, r1=0.0), "fixed") is None
    assert exit_return(_mday([(1, 1, 1)] * 27, sigma=None), "tp_0.5") is None


def test_minute_days_builds_r1_entry_and_sigma_from_the_kept_minutes():
    from datetime import UTC, datetime, timedelta

    from app.lab.desk_replay import NEW_YORK, evaluate_exits, minute_days, Costs
    rows = []
    d0 = datetime(2026, 6, 1, tzinfo=NEW_YORK)
    k = 0
    for i in range(40):
        d = d0 + timedelta(days=i)
        if d.weekday() > 4:
            continue
        k += 1
        base = 100 + (0.5 if k % 2 else -0.5)
        stamp = lambda hh, mm: (d.replace(hour=hh, minute=mm)).astimezone(UTC)  # noqa: E731
        rows.append((stamp(9, 59), base, base, base * 1.001))
        for m in range(29, 60):
            rows.append((stamp(15, m), base + 0.1, base - 0.1, base))
    days = minute_days(rows)
    assert days and days[-1].sigma is not None and len(days[-1].path) == 27
    rep = evaluate_exits(days, Costs(), desk_days=False)
    assert set(rep["variants"]) == {"fixed", "tp_0.5", "tp_1.0", "trail"}
    assert "p_holm" in rep["variants"]["tp_0.5"]["vs_fixed"]


def test_minute_days_ignores_after_hours_bars():
    from datetime import UTC, datetime, timedelta

    from app.lab.desk_replay import NEW_YORK, minute_days

    def build(after_hours):
        rows, k = [], 0
        for i in range(40):
            d = datetime(2026, 6, 1, tzinfo=NEW_YORK) + timedelta(days=i)
            if d.weekday() > 4:
                continue
            k += 1
            base = 100 + (0.5 if k % 2 else -0.5)
            for hh, mm in [(9, 59)] + [(15, m) for m in range(29, 60)]:
                rows.append((d.replace(hour=hh, minute=mm).astimezone(UTC), base, base, base))
            if after_hours:
                rows.append((d.replace(hour=19, minute=59).astimezone(UTC), 150.0, 150.0, 150.0))
        return minute_days(rows)

    assert [(x.r1, x.sigma) for x in build(True)] == [(x.r1, x.sigma) for x in build(False)]
