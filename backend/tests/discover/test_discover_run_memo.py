"""One read per run of what every candidate shares (plan 2026-11-08, WP1/WP2)."""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.decision.discover import pipeline


def _rows(n_days: int):
    today = date.today()
    return [
        {"date": today - timedelta(days=n_days - i), "close": 100.0 + i, "stale": False}
        for i in range(n_days)
    ]


class _Counter:
    def __init__(self, n_days=1000):
        self.calls: list[tuple[str, int]] = []
        self.n_days = n_days

    def __call__(self, db, symbol, days=365, **_kw):
        self.calls.append((symbol, days))
        cutoff = date.today() - timedelta(days=days)
        return [r for r in _rows(self.n_days) if r["date"] >= cutoff]


def _in_run(fn):
    memo = pipeline._RunMemo()
    pipeline._RUN_STATE.memo = memo
    try:
        return fn(), memo
    finally:
        pipeline._RUN_STATE.memo = None


def test_outside_a_run_every_call_reads(monkeypatch):
    counter = _Counter()
    monkeypatch.setattr(pipeline, "market_history", counter)
    pipeline._history(None, "AAPL", days=400)
    pipeline._history(None, "AAPL", days=400)
    assert len(counter.calls) == 2


def test_in_a_run_a_shorter_window_is_cut_from_the_longer_one(monkeypatch):
    counter = _Counter()
    monkeypatch.setattr(pipeline, "market_history", counter)

    def run():
        long = pipeline._history(None, "aapl", days=900)
        short = pipeline._history(None, "AAPL", days=365)
        return long, short

    (long, short), memo = _in_run(run)
    assert counter.calls == [("aapl", 900)]
    # Exactly what a direct read of the shorter window returns.
    assert short == counter(None, "AAPL", days=365)
    assert len(long) > len(short)
    assert memo.history_reads == 1 and memo.history_hits == 1


def test_a_longer_window_reads_again(monkeypatch):
    counter = _Counter()
    monkeypatch.setattr(pipeline, "market_history", counter)
    _in_run(lambda: (pipeline._history(None, "X", days=365), pipeline._history(None, "X", days=730)))
    assert counter.calls == [("X", 365), ("X", 730)]


def test_cached_rows_cannot_be_changed_by_a_caller(monkeypatch):
    monkeypatch.setattr(pipeline, "market_history", _Counter())

    def run():
        first = pipeline._history(None, "X", days=30)
        first[0]["close"] = -1.0
        return pipeline._history(None, "X", days=30)

    second, _ = _in_run(run)
    assert second[0]["close"] != -1.0


def test_an_ingest_makes_the_next_read_fresh(monkeypatch):
    counter = _Counter()
    monkeypatch.setattr(pipeline, "market_history", counter)

    def run():
        pipeline._history(None, "X", days=365)
        pipeline._forget_history("x")
        pipeline._history(None, "X", days=365)

    _in_run(run)
    assert len(counter.calls) == 2


def test_the_book_is_built_once_for_all_candidates(monkeypatch):
    weeks = pd.date_range(end=pd.Timestamp.today().normalize(), periods=110, freq="W-FRI")
    rng = np.random.default_rng(1)
    book = pd.DataFrame({"EUNL.DE": 100 * np.cumprod(1 + rng.normal(0, 0.02, len(weeks)))}, index=weeks)
    builds: list[str] = []

    def build(db, user_id, lookback_days=365):
        builds.append(user_id)
        return book

    cand_rows = [
        {"date": d.date(), "close": float(c)}
        for d, c in zip(weeks, 50 * np.cumprod(1 + rng.normal(0, 0.03, len(weeks))))
    ]
    monkeypatch.setattr(pipeline, "build_real_price_matrix", build)
    monkeypatch.setattr(pipeline, "_book_weights", lambda db, user_id, tickers: None)
    monkeypatch.setattr(pipeline, "market_history", lambda db, symbol, days=365, **_kw: cand_rows)
    monkeypatch.setattr(pipeline, "_closes_in_eur", lambda db, symbol, df, **_kw: (df, "eur"))

    def run():
        return [pipeline.stage_portfolio_fit(None, "u1", sym)[0] for sym in ("A", "B", "C")]

    fits, _ = _in_run(run)
    assert builds == ["u1"]
    assert all(f["correlation"] is not None for f in fits)
    # Outside a run each call builds, as before.
    pipeline.stage_portfolio_fit(None, "u1", "A")
    assert builds == ["u1", "u1"]


def test_holdings_currencies_are_warmed(monkeypatch):
    warmed: list[str] = []
    monkeypatch.setattr(pipeline, "_preingest_one", lambda sym: (warmed.append(sym), (sym, True))[1])
    pipeline._preingest_candidates(["AAPL"], fx_symbols=["CNQ.TO", "RY.TO", "EUNL.DE"])
    # No USDCAD=X was ever stored: the four Canadian holdings fell out of the fit.
    assert "USDCAD=X" in warmed
    assert "CNQ.TO" not in warmed  # a holding's own bars are not part of the warm-up


def test_the_run_wrapper_sets_and_clears_the_memo(monkeypatch):
    seen = []

    def body(*args, **kwargs):
        seen.append(pipeline._run_memo())
        pipeline._timed("stage", lambda: None)
        yield {"index": 1}
        return ["picked"]

    monkeypatch.setattr(pipeline, "_candidate_pipeline", body)
    gen = pipeline.run_candidate_pipeline(None, "u1", [])
    progress = list(_drain(gen))
    assert progress[:-1] == [{"index": 1}] and progress[-1] == ["picked"]
    assert isinstance(seen[0], pipeline._RunMemo) and "stage" in seen[0].timings
    assert pipeline._run_memo() is None


def _drain(gen):
    while True:
        try:
            yield next(gen)
        except StopIteration as stop:
            yield stop.value
            return
