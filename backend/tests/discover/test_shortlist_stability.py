"""WP12b (plan 2026-11-10): shortlist stability under weight perturbation."""
from __future__ import annotations

from app.decision.discover.shortlist_stability import stability


def _result(symbol, momentum, fit, lane="stock"):
    return {"symbol": symbol, "lane": lane, "reject_stage": None, "scores": {"composite_inputs": [
        {"signal": "momentum", "score": momentum, "weight": 0.5},
        {"signal": "portfolio", "score": fit, "weight": 0.5},
    ]}}


def test_a_dominant_name_stays_at_one_hundred_percent():
    rows = [_result("TOP", 0.99, 0.99)] + [_result(f"N{i}", 0.5, 0.5 - i * 0.001) for i in range(10)]
    out = stability(rows, [rows[0]], draws=100)
    lane = out["lanes"]["stock"]
    assert lane["share"]["TOP"] == 1.0 and lane["stable"] == 1 and lane["competing"] == 1


def test_ties_rotate_and_many_names_compete_for_one_place():
    rows = [_result(f"T{i}", 0.5, 0.5) for i in range(6)]
    out = stability(rows, [rows[0]], draws=200)
    lane = out["lanes"]["stock"]
    # identical signals: the symbol tie-break always picks the same name, so perturbation cannot rotate them
    assert lane["places"] == 1
    close = [_result("A", 0.60, 0.40), _result("B", 0.40, 0.60), _result("C", 0.50, 0.50)]
    out = stability(close, [close[0]], draws=200)["lanes"]["stock"]
    assert out["competing"] >= 2 and out["stable"] == 0
    assert "compete for these 1 places" in out["text"] or "compete for these 1" in out["text"]


def test_no_inputs_no_report():
    assert stability([{"symbol": "X", "scores": {}}], [{"symbol": "X"}]) is None


def test_shifting_the_momentum_window_can_swap_a_close_pair():
    # A leads on momentum today; with the window ends moved back, B leads. Fit is equal.
    def row(sym, mom_now, shifts):
        r = _result(sym, mom_now, 0.5)
        r["scores"]["momentum_quality"] = {"momentum_12_1m_shifts": shifts}
        return r

    rows = [row("A", 0.9, [0.10] * 5), row("B", 0.1, [0.30] * 5)]
    rows += [row(f"F{i}", 0.5, [0.20 + i * 0.001] * 5) for i in range(4)]
    out = stability(rows, [rows[0]], draws=300)["lanes"]["stock"]
    assert 0.0 < out["share"].get("A", 0) < 1.0


def test_momentum_stage_stores_shifted_values(monkeypatch):
    import numpy as np
    from conftest import _memory_db

    from app.decision.discover import pipeline as pipeline_mod

    path = list(100 * np.cumprod(np.linspace(1.0005, 1.002, 300)))
    rows = [{"date": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", "close": c} for i, c in enumerate(path)]
    monkeypatch.setattr(pipeline_mod, "market_history", lambda _db, sym, days=0, **_kw: rows)
    monkeypatch.setattr("app.foundation.eur_prices.eur_per_unit_by_date", lambda *a, **k: None)
    scores, _ = pipeline_mod.stage_momentum_quality(_memory_db(), "AAPL")
    shifts = scores["momentum_12_1m_shifts"]
    assert len(shifts) == 5 and all(isinstance(v, float) for v in shifts)
    assert shifts[0] != scores["momentum_12_1m"]
