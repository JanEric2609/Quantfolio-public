"""Ingest guard for daily bars (plan 2026-10-12, D2 / WP1b)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.foundation.core.db import Base
from app.foundation.data_backbone.bar_guard import confirms, guard_bars, is_session
from app.foundation.data_backbone.ingest import DataIngester
from app.foundation.models.entities import Asset, BarQuarantine, BarSeriesMeta
from conftest import _create_ormless_tables


def _memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    _create_ormless_tables(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _frame(closes: list[float], start: str = "2025-10-20", provider: str = "yfinance", freq: str = "B") -> pd.DataFrame:
    days = pd.bdate_range(start, periods=len(closes)) if freq == "B" else pd.date_range(start, periods=len(closes))
    return pd.DataFrame({"ts": days, "close": closes, "provider": provider})


def test_p500_100_to_1_split_is_flagged_and_quarantined():
    # P500.DE, 2025-12-15: 1,000 -> 10 with no reversal.
    bars = _frame([1000.0, 1004.0, 10.05, 10.1, 10.0], start="2025-12-11")
    result = guard_bars(bars, symbol="P500.DE", guard_class="equity_etf", pinned_provider="yfinance")
    assert result.split_suspected
    assert [q.reason for q in result.quarantined] == ["split_signature"] * 3
    assert len(result.accepted) == 2


def test_split_is_only_flagged_on_the_repull_path():
    bars = _frame([1000.0, 1004.0, 10.05, 10.1], start="2025-12-11")
    result = guard_bars(bars, symbol="P500.DE", guard_class="equity_etf", allow_split=True)
    assert result.split_suspected
    assert result.quarantined == []
    assert len(result.accepted) == 4


def test_2025_10_24_spike_and_reversal_is_quarantined():
    # I500/SGAS/8PSG/MWOE/P500: +17 % on 2025-10-24, -13 % the next day.
    bars = _frame([100.0, 100.5, 101.0, 100.8, 117.9, 102.6, 102.9], start="2025-10-20")
    result = guard_bars(bars, symbol="I500.DE", guard_class="equity_etf", pinned_provider="yfinance")
    assert [(q.ts.date().isoformat(), q.reason) for q in result.quarantined] == [("2025-10-24", "spike_reversal")]
    assert len(result.accepted) == 6


def test_real_index_crash_without_reversal_is_accepted():
    # SPY 2020-03-16: -12 % and no reversal the next day.
    bars = _frame([270.0, 239.8, 252.8, 240.0], start="2020-03-13")
    result = guard_bars(bars, symbol="SPY", guard_class="equity_etf", pinned_provider="tiingo")
    assert result.quarantined == []
    assert len(result.accepted) == 4


def test_christmas_day_bar_is_dropped():
    bars = pd.DataFrame({
        "ts": pd.to_datetime(["2025-12-24", "2025-12-25", "2025-12-26"]),
        "close": [600.0, 601.0, 602.0],
        "provider": "unknown",
    })
    result = guard_bars(bars, symbol="SPY", guard_class="equity_etf")
    assert [(q.ts.date().isoformat(), q.reason) for q in result.quarantined] == [("2025-12-25", "non_session")]
    assert not is_session("SPY", pd.Timestamp("2025-12-25"))
    assert is_session("^VIX", pd.Timestamp("2025-12-25"))  # no calendar: never dropped


def test_bond_etf_six_percent_spike_is_quarantined_but_stock_is_not():
    closes = [50.0, 50.02, 53.0, 50.05, 50.03]
    bond = guard_bars(_frame(closes), symbol="CBE3.DE", guard_class="bond_mm_etf")
    assert [q.reason for q in bond.quarantined] == ["spike_reversal"]
    stock = guard_bars(_frame(closes), symbol="AAPL", guard_class="single_stock")
    assert stock.quarantined == []


def test_stored_bar_is_moved_when_the_next_ingest_shows_the_reversal():
    stored = _frame([100.0, 100.5, 117.9], start="2025-10-22")
    new = _frame([102.6, 102.9], start="2025-10-27")
    result = guard_bars(new, stored, symbol="SGAS.DE", guard_class="equity_etf", pinned_provider="yfinance")
    assert [(q.ts.date().isoformat(), q.stored) for q in result.quarantined] == [("2025-10-24", True)]
    assert len(result.accepted) == 2


def test_off_scale_bar_from_another_provider():
    bars = _frame([100.0, 101.0, 130.0, 131.0], provider="yfinance")
    bars.loc[2:, "provider"] = "eod"
    result = guard_bars(bars, symbol="VWCE.DE", guard_class="equity_etf", pinned_provider="yfinance")
    # Every eod bar sits off the pinned scale, not only the first.
    assert [q.reason for q in result.quarantined] == ["provider_scale", "provider_scale"]
    assert len(result.accepted) == 2


def test_second_provider_confirmation_tolerance():
    assert confirms(117.9, 117.0)
    assert not confirms(117.9, 101.0)
    assert not confirms(117.9, None)


def _ingester(db, history: dict, confirm: dict | None = None, splits: list | None = None) -> DataIngester:
    ingester = DataIngester.__new__(DataIngester)
    ingester.db = db
    ingester.registry = MagicMock()
    # The provider's split events, never fetched live in a test (None = unreadable).
    ingester.split_events = lambda symbol: splits  # type: ignore[method-assign]

    def _history(symbol, start=None, end=None, days=None, provider=None):
        if provider not in (None, history["provider"]) and confirm is not None:
            return confirm
        return history

    ingester.registry.get_price_history.side_effect = _history
    return ingester


def _etf_db():
    db = _memory_db()
    db.add(Asset(symbol="SGAS.DE", name="iShares Core S&P 500 UCITS ETF", asset_type="etf"))
    db.commit()
    return db


def _rows(closes: list[float], start: str) -> list[dict]:
    return [
        {"date": d.date().isoformat(), "open": c, "high": c, "low": c, "close": c, "volume": 1}
        for d, c in zip(pd.bdate_range(start, periods=len(closes)), closes)
    ]


def test_ingest_quarantines_a_spike_and_pins_the_provider():
    db = _etf_db()
    history = {"ok": True, "provider": "yfinance", "data": _rows([100.0, 100.5, 117.9, 102.6, 102.9], "2025-10-22")}
    result = _ingester(db, history, confirm={"ok": False}).ingest_bar_prices("SGAS.DE", days=10)
    assert result["success"] and result["rows_inserted"] == 4 and result["quarantined"] == 1
    assert db.get(BarSeriesMeta, "SGAS.DE").provider == "yfinance"
    q = db.query(BarQuarantine).one()
    assert (q.reason, q.resolved_at) == ("spike_reversal", None)
    stored = db.execute(text("SELECT count(*) FROM bar_prices WHERE symbol='SGAS.DE'")).scalar()
    assert stored == 4


def test_a_confirming_second_provider_releases_the_bar():
    db = _etf_db()
    rows = _rows([100.0, 100.5, 117.9, 102.6, 102.9], "2025-10-22")
    history = {"ok": True, "provider": "yfinance", "data": rows}
    confirm = {"ok": True, "provider": "tiingo", "data": rows}
    result = _ingester(db, history, confirm=confirm).ingest_bar_prices("SGAS.DE", days=10)
    assert result["rows_inserted"] == 5 and result["quarantined"] == 0
    q = db.query(BarQuarantine).one()
    assert q.resolution == "confirmed:second_provider" and q.resolved_at is not None


def test_pinned_provider_failure_does_not_fall_back():
    db = _memory_db()
    db.add(BarSeriesMeta(symbol="SPY", provider="tiingo", adjusted=True))
    db.commit()
    ingester = DataIngester.__new__(DataIngester)
    ingester.db = db
    ingester.registry = MagicMock()
    ingester.registry.get_price_history.return_value = {"ok": False, "error": "HTTP 500"}
    result = ingester.ingest_bar_prices("SPY", days=5)
    assert not result["success"]
    assert "no fallback" in result["message"]
    _, kwargs = ingester.registry.get_price_history.call_args
    assert kwargs["provider"] == "tiingo"


def test_repull_replaces_the_series_and_moves_the_pin():
    db = _memory_db()
    db.execute(text(
        "INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider) "
        "VALUES ('SPY', '2021-10-12 00:00:00', 1, 1, 1, 434.0, 1, 'USD', 'twelvedata')"
    ))
    db.add(BarSeriesMeta(symbol="SPY", provider="twelvedata", adjusted=False))
    db.commit()
    history = {"ok": True, "provider": "tiingo", "data": _rows([400.0, 401.0, 402.0], "2021-10-12")}
    result = _ingester(db, history).repull_series("SPY", provider="tiingo")
    assert result["success"] and result["rows_inserted"] == 3
    providers = {r[0] for r in db.execute(text("SELECT provider FROM bar_prices WHERE symbol='SPY'"))}
    assert providers == {"tiingo"}
    pin = db.get(BarSeriesMeta, "SPY")
    assert pin.provider == "tiingo" and pin.adjusted and pin.last_full_repull_at is not None


def test_split_that_survives_a_repull_marks_the_series():
    db = _memory_db()
    history = {"ok": True, "provider": "yfinance", "data": _rows([1000.0, 1004.0, 10.05, 10.1], "2025-12-11")}
    result = _ingester(db, history).ingest_bar_prices("P500.DE", days=10)
    assert result["repull"] and result["split_unadjusted"]
    assert db.get(BarSeriesMeta, "P500.DE").quarantine_reason == "split_unadjusted"


def test_discover_skips_a_series_marked_split_unadjusted():
    from app.decision.discover.pipeline import stage_history_ingest

    db = _memory_db()
    db.add(BarSeriesMeta(symbol="P500.DE", provider="yfinance", adjusted=True, quarantine_reason="split_unadjusted"))
    db.commit()
    scores, reason = stage_history_ingest(db, "P500.DE")
    assert scores is None and "split_unadjusted" in (reason or "")


def test_a_truncated_repull_keeps_the_stored_series():
    # 2026-10-12: yfinance returned 1 bar for AVB ("possibly delisted") over a
    # 27-bar stored window; the re-pull would have deleted the other 26.
    db = _memory_db()
    for day in range(10):
        db.execute(text(
            "INSERT INTO bar_prices (symbol, ts, open, high, low, close, volume, currency, provider) "
            f"VALUES ('AVB', '2026-08-{day + 10:02d} 00:00:00', 1, 1, 1, 65.0, 1, 'USD', 'yfinance')"
        ))
    db.add(BarSeriesMeta(symbol="AVB", provider="yfinance", adjusted=True))
    db.commit()
    history = {"ok": True, "provider": "yfinance", "data": _rows([184.06], "2026-08-14")}
    result = _ingester(db, history).repull_series("AVB", provider="yfinance")
    assert not result["success"] and "stored series kept" in result["message"]
    assert db.execute(text("SELECT count(*) FROM bar_prices WHERE symbol='AVB'")).scalar() == 10
    assert db.get(BarSeriesMeta, "AVB").last_full_repull_at is None


def _stored_closes(db, symbol: str) -> list[float]:
    rows = db.execute(text("SELECT close FROM bar_prices WHERE symbol = :s ORDER BY ts"), {"s": symbol}).all()
    return [float(r[0]) for r in rows]


def test_a_spike_caught_on_the_incremental_overlap_leaves_bar_prices():
    # Plan 2026-10-14, H3: the spike is stored first (no reversal yet); the
    # next fetch starts one day back, so it returns as a "new" bar.
    db = _etf_db()
    first = {"ok": True, "provider": "yfinance", "data": _rows([100.0, 100.5, 117.9], "2025-10-22")}
    _ingester(db, first, confirm={"ok": False}).ingest_bar_prices("SGAS.DE", days=10)
    assert _stored_closes(db, "SGAS.DE") == [100.0, 100.5, 117.9]

    overlap = {"ok": True, "provider": "yfinance", "data": _rows([117.9, 102.6, 102.9], "2025-10-24")}
    result = _ingester(db, overlap, confirm={"ok": False}).ingest_bar_prices("SGAS.DE", days=3)
    assert result["quarantined"] == 1
    assert _stored_closes(db, "SGAS.DE") == [100.0, 100.5, 102.6, 102.9]


def test_a_quarantined_fresh_bar_does_not_delete_a_different_stored_bar():
    from app.foundation.data_backbone.bar_guard import QuarantinedBar

    db = _etf_db()
    history = {"ok": True, "provider": "yfinance", "data": _rows([100.0, 100.5], "2025-10-22")}
    ingester = _ingester(db, history, confirm={"ok": False})
    ingester.ingest_bar_prices("SGAS.DE", days=10)
    day = pd.Timestamp("2025-10-23")
    # Another provider's bar for the same day, off the pinned scale.
    ingester._write_quarantine("SGAS.DE", [QuarantinedBar(day, None, None, None, 130.0, "eod", "provider_scale")])
    db.commit()
    assert _stored_closes(db, "SGAS.DE") == [100.0, 100.5]


# --- M1 (plan 2026-10-14): a crash is not a split -------------------------------


def test_a_fifty_percent_crash_without_a_split_event_stays_in():
    db = _memory_db()
    history = {"ok": True, "provider": "yfinance", "data": _rows([40.0, 40.4, 20.1, 19.8, 20.3], "2025-12-11")}
    result = _ingester(db, history, splits=[]).ingest_bar_prices("XYZ", days=10)
    assert result["repull"] and result["split_verdict"] == "real_move" and not result["split_unadjusted"]
    pin = db.get(BarSeriesMeta, "XYZ")
    assert pin.quarantine_reason is None and pin.notes.startswith("real_move")
    assert _stored_closes(db, "XYZ") == [40.0, 40.4, 20.1, 19.8, 20.3]


def test_a_listed_split_is_still_caught():
    db = _memory_db()
    # yfinance lists the 100:1 split as factor 100 on the day.
    splits = [(pd.Timestamp("2025-12-15"), 100.0)]
    history = {"ok": True, "provider": "yfinance", "data": _rows([1000.0, 1004.0, 10.05, 10.1], "2025-12-11")}
    result = _ingester(db, history, splits=splits).ingest_bar_prices("P500.DE", days=10)
    assert result["split_verdict"] == "split" and result["split_unadjusted"]
    assert db.get(BarSeriesMeta, "P500.DE").quarantine_reason == "split_unadjusted"


def test_a_two_for_one_split_listed_near_the_day_is_a_split():
    db = _memory_db()
    splits = [(pd.Timestamp("2025-12-12"), 2.0)]  # two sessions before the jump
    history = {"ok": True, "provider": "yfinance", "data": _rows([40.0, 40.4, 20.1, 19.8, 20.3], "2025-12-11")}
    result = _ingester(db, history, splits=splits).ingest_bar_prices("XYZ", days=10)
    assert result["split_verdict"] == "split"


def test_a_hundredfold_jump_without_an_event_stays_a_split():
    db = _memory_db()
    history = {"ok": True, "provider": "yfinance", "data": _rows([1000.0, 1004.0, 10.05, 10.1], "2025-12-11")}
    result = _ingester(db, history, splits=[]).ingest_bar_prices("P500.DE", days=10)
    assert result["split_verdict"] == "split" and result["split_unadjusted"]
