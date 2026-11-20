"""How much of a candidate's score rested on real data, and which signals a run lacked.

``Recommendation.data_quality_score`` used to be the price-history coverage
alone, so it read 1.00 on all 15 dossiers of the 2026-11-05 run although the SEC
refresh had failed and no stock had insider data (Discover audit, 2026-11-06).
A missing signal renormalises away inside the composite, so nothing else showed
it. Pure: no DB, no network.
"""
from __future__ import annotations

from typing import Any

from app.decision.discover.composite import WEIGHTS

#: Concern set when book equity is too small for P/B, ROE and debt/equity to mean anything.
BOOK_EQUITY_CONCERN = "book_equity_near_zero"
#: The only signals that apply to a fund: it has no analysts, insiders or earnings.
FUND_SIGNALS = frozenset({"momentum", "risk", "benchmark", "portfolio"})
#: Below this share of a run's stocks, a signal counts as missing from the run.
MIN_RUN_COVERAGE = 0.10

SIGNAL_TEXT = {
    "ic_icir": "validated alpha factors",
    "analyst": "analyst consensus",
    "sentiment": "news sentiment",
    "portfolio": "fit with your portfolio",
    "fundamentals": "fundamentals",
    "momentum": "momentum",
    "risk": "risk",
    "benchmark": "performance against the benchmark",
    "ml_signal": "the ML model",
    "estimate_revision": "estimate revisions",
    "insider_signal": "insider trading",
}


def _present(scores: dict[str, Any]) -> set[str]:
    inputs = scores.get("composite_inputs")
    return {str(row["signal"]) for row in inputs if isinstance(row, dict)} if isinstance(inputs, list) else set()


def stamp_data_quality(results: list[dict[str, Any]], weights: dict[str, float] | None = None) -> None:
    """Stamp ``scores["data_quality"]`` on each scored result of one run.

    ``weight_share`` is the share of the weight of the signals that apply to
    this instrument that had data. ``missing`` names the absent ones and
    ``run_gaps`` those almost no stock in the run had: a data problem of the
    run rather than of the name.
    """
    w = {**WEIGHTS, **{k: float(v) for k, v in (weights or {}).items() if k in WEIGHTS}}
    stocks = [r for r in results if r.get("lane") != "fund" and isinstance(r.get("scores"), dict)]
    present_by_stock = [_present(r["scores"]) for r in stocks]
    run_gaps = sorted(
        s for s in w
        if stocks and w[s] > 0 and sum(s in p for p in present_by_stock) / len(stocks) < MIN_RUN_COVERAGE
    )
    for result in results:
        scores = result.get("scores")
        if not isinstance(scores, dict):
            continue
        applicable = FUND_SIGNALS if result.get("lane") == "fund" else frozenset(w)
        present = _present(scores)
        total = sum(w[s] for s in applicable if w.get(s, 0) > 0)
        scored = sum(w[s] for s in applicable if s in present and w.get(s, 0) > 0)
        missing = sorted(s for s in applicable if s not in present and w.get(s, 0) > 0)
        scores["data_quality"] = {
            "weight_share": round(scored / total, 4) if total > 0 else 0.0,
            "missing": [SIGNAL_TEXT.get(s, s) for s in missing],
            "run_gaps": [SIGNAL_TEXT.get(s, s) for s in run_gaps if s in applicable],
        }


def data_quality_score(history_coverage: Any, data_quality: Any) -> float:
    """Price-history coverage times the share of signal weight with data, 0-1."""
    coverage = float(history_coverage) if isinstance(history_coverage, (int, float)) else 0.0
    share = data_quality.get("weight_share") if isinstance(data_quality, dict) else None
    # A run scored before this stamp existed has only its coverage.
    return round(coverage * (float(share) if isinstance(share, (int, float)) else 1.0), 2)
