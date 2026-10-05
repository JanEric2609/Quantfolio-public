"""Test that the discover pre-ingest phase reports progress.

This guards against the "stuck at 0/N" symptom where the (potentially
multi-minute) bar-price warm-up emitted no progress to the UI.
"""
from __future__ import annotations

from app.decision.discover import pipeline


def test_preingest_invokes_progress_callback(monkeypatch):
    # Avoid any network / DB work inside the worker.
    monkeypatch.setattr(pipeline, "_preingest_one", lambda sym: (sym, True))

    # M0: warm-up covers candidates PLUS the benchmark ETFs (appended after).
    candidates = ["AAA", "BBB", "CCC", "DDD"]
    expected = [
        *candidates,
        pipeline._BENCHMARK_EUROPE,
        pipeline._BENCHMARK_US,
        pipeline._BENCHMARK_GLOBAL_ETF,
        pipeline._BENCHMARK_JAPAN,
    ]
    seen: list[tuple[int, int, str]] = []

    pipeline._preingest_candidates(candidates, progress=lambda done, total, sym: seen.append((done, total, sym)))

    # One callback per symbol, monotonically increasing `done`, correct total.
    assert len(seen) == len(expected)
    assert [d for d, _, _ in seen] == list(range(1, len(expected) + 1))
    assert all(total == len(expected) for _, total, _ in seen)
    assert {sym for _, _, sym in seen} == set(expected)


def test_preingest_empty_is_noop(monkeypatch):
    called = False

    def _boom(sym):  # pragma: no cover - must not be called
        nonlocal called
        called = True
        return (sym, True)

    monkeypatch.setattr(pipeline, "_preingest_one", _boom)
    pipeline._preingest_candidates([], progress=lambda *a: None)
    assert called is False
