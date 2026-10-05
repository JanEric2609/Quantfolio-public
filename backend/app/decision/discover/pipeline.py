"""Discover pipeline: 5-stage deterministic scoring for recommendation candidates.

Each stage is a pure function that returns (scores dict, reject_reason_or_None).
The composite orchestrator ranks survivors by a weighted score and returns
top 3-5 shortlisted candidates.

No stage writes to the database; the caller (orchestrator) handles persistence.
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
import threading
from pathlib import Path
from collections.abc import Generator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import pandas as pd
from sqlalchemy.orm import Session

from app.foundation.core.db import SessionLocal
from app.foundation.data_backbone.ingest import DataIngester
from app.foundation.data_engineering.pit_panel_joins import (
    pit_ibes_estimate_signal_for_symbol,
    pit_insider_signal_for_symbol,
    pit_wrds_factor_characteristics_for_symbol,
)
from app.decision.discover.composite import (
    composite_contributions,
    compute_weighted_composite,
    derive_signals_from_scores,
)
from app.decision.discover.config import get_or_seed_active_config
from app.decision.discover.listing import is_eu_listing
from app.foundation.data_backbone.listing_currency import resolve_currency
from app.decision.discover.regime_gate import MacroRegimeGate
from app.foundation.etf_overlap import pairwise_overlap
from app.foundation.instrument_taxonomy import classify_instrument
from app.foundation.market import history as market_history, refresh_stale_bars
from app.foundation.portfolio.bridge import build_real_price_matrix
from app.foundation.providers.registry import build_provider_registry
from app.foundation.providers.utils import listing_region
from app.foundation.security_master import resolve_securities_batch
from app.foundation.quant_metrics import (
    annualised_return,
    annualised_volatility,
    historical_cvar,
    historical_var,
    max_drawdown,
    sharpe_ratio,
)
from app.foundation.quant_metrics import beta as beta_vs_benchmark

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> float | None:
    """Coerce a value to float, returning None for missing/invalid values.

    NaN and infinity are missing values too. They used to come back as 0.0,
    so an unknown IBES revision read as "no revision" and an unknown insider
    window as "no insider trading", each weighted into the composite as
    neutral evidence.
    """
    if isinstance(value, bool):
        return float(value)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


# Maximum composite-score reduction for a ticker recommended today (day 0);
# halves every `discover_recency_penalty_halflife_days` public setting, so a
# candidate re-suggested a full half-life ago carries half this scale, one
# half-life further only a quarter, etc. Deliberately capped well below 1.0
# — this is a tie-breaker against staleness, not a veto: a genuinely strong
# signal should still be able to outrank a weak, merely-unseen one.
_RECENCY_PENALTY_MAX_SCALE = 0.25
_RECENCY_PENALTY_DEFAULT_HALFLIFE_DAYS = 30.0


def _recency_penalty_halflife_days(db: Session) -> float:
    from app.foundation.settings import get_public_settings

    settings = get_public_settings(db)
    try:
        halflife = float(
            settings.get(
                "discover_recency_penalty_halflife_days",
                _RECENCY_PENALTY_DEFAULT_HALFLIFE_DAYS,
            )
        )
    except (TypeError, ValueError):
        return _RECENCY_PENALTY_DEFAULT_HALFLIFE_DAYS
    return halflife if halflife > 0 else _RECENCY_PENALTY_DEFAULT_HALFLIFE_DAYS


# A recently recommended name escapes the penalty only when its pre-penalty
# composite has risen by at least this much since the last recommendation:
# re-suggesting it is then justified by new evidence, not by habit.
_RECENCY_EXEMPT_IMPROVEMENT = 0.05


def _apply_recency_penalty(
    db: Session,
    user_id: str,
    symbol: str,
    instrument_type: str,
    composite: float,
) -> tuple[float, dict[str, Any]]:
    """Apply an exponential-decay penalty for re-suggesting a recently
    recommended individual stock, so the discover shortlist doesn't converge
    on the same handful of names run after run.

    Scoped to single-name equities only (``instrument_type == "equity"``) —
    a diversified ETF isn't "the same idea" the way a repeated stock ticker
    is. *composite* is the pre-penalty score.

    The exemption used to be ``composite >= STRONG_CONVICTION_THRESHOLD``
    (0.65), but every name that reached the 2026-09-25 shortlist scored at
    least 0.68, so the penalty never fired where it mattered: BBVA.MC was
    shortlisted on Sep 7, 15, 24 and 25 (ADR 0017). A level threshold cannot
    work here, because the shortlist is by definition the top of the score
    distribution. The exemption is now relative: the name must score at
    least ``_RECENCY_EXEMPT_IMPROVEMENT`` above its own pre-penalty composite
    at the last recommendation. That last recommendation is always looked
    up, so ``days_since_last`` is populated even when the name is exempt.

    Returns ``(adjusted_composite, detail)``. ``detail`` is always populated
    for equities and empty for every other instrument type.
    """
    if instrument_type != "equity":
        return composite, {}

    from app.foundation.models.entities import DiscoveryPrediction

    last = (
        db.query(DiscoveryPrediction)
        .filter(DiscoveryPrediction.user_id == user_id, DiscoveryPrediction.symbol == symbol)
        .order_by(DiscoveryPrediction.predicted_at.desc())
        .first()
    )
    if last is None:
        return composite, {
            "penalty_fraction": 0.0,
            "days_since_last": None,
            "last_composite": None,
            "exempt_reason": "never_recommended",
        }

    predicted_at = last.predicted_at
    if predicted_at.tzinfo is None:
        predicted_at = predicted_at.replace(tzinfo=UTC)
    days_since_last = max(0.0, (datetime.now(UTC) - predicted_at).total_seconds() / 86400.0)

    # Pre-penalty composite of the last recommendation. Rows written before
    # composite_raw existed only carry the post-penalty conviction, which
    # would make a penalised name look "improved" on its next run, so they
    # never qualify for the exemption.
    breakdown = (last.features_json or {}).get("signal_breakdown") or {}
    last_raw = breakdown.get("composite_raw") if isinstance(breakdown, dict) else None
    last_composite = float(last_raw) if isinstance(last_raw, (int, float)) else None

    detail: dict[str, Any] = {
        "days_since_last": round(days_since_last, 1),
        "last_composite": round(last_composite, 4) if last_composite is not None else None,
    }
    if last_composite is not None and composite >= last_composite + _RECENCY_EXEMPT_IMPROVEMENT:
        return composite, {**detail, "penalty_fraction": 0.0, "exempt_reason": "improved_since_last"}

    halflife_days = _recency_penalty_halflife_days(db)
    penalty_fraction = _RECENCY_PENALTY_MAX_SCALE * (0.5 ** (days_since_last / halflife_days))
    adjusted = composite * (1.0 - penalty_fraction)
    return adjusted, {**detail, "penalty_fraction": round(penalty_fraction, 4), "exempt_reason": None}


# AlphaCrafter is experimental and may not be installed/available. Import
# cautiously so the discover pipeline keeps working when it is absent.
try:
    from app.lab.alphacrafter import miner as alpha_miner_module
    from app.lab.alphacrafter import screener as alpha_screener_module
    from app.lab.alphacrafter.panel import build_panel as alpha_build_panel

    _ALPHACRAFTER_AVAILABLE = True
except Exception as _alpha_import_exc:  # pragma: no cover
    alpha_miner_module = None
    alpha_screener_module = None
    alpha_build_panel = None
    _ALPHACRAFTER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Pre-ingest helper — parallel history warm-up
# ---------------------------------------------------------------------------

_PREINGEST_MAX_WORKERS = 8
_PREINGEST_DAYS = 365 * 5  # match stage_history_ingest
_PREINGEST_TIMEOUT_S = float(os.getenv("PREINGEST_TIMEOUT_S") or "120")

# Number of top-ranked candidates handed to the LLM agent for deep evaluation.
# In agentic mode the deterministic stages no longer hard-reject on quality; they
# rank every candidate and the best N go to the model (bounded for local-LLM cost).
LLM_MAX_CANDIDATES = int(os.getenv("DISCOVER_LLM_MAX_CANDIDATES") or "15")

# Advisory quality thresholds. Breaching these no longer rejects a candidate — it
# records a "concern" flag that lowers the composite rank and is surfaced to the LLM.
_MOMENTUM_MIN = -0.20
_VOL_MAX = 0.60
_EXCESS_MIN = -0.10
_SHARPE_DELTA_MIN = -0.5
_MDD_MAX = 0.30

# Region-aware benchmark mapping by listing suffix. Values are liquid, USD-listed
# ETFs the provider chain can resolve (same path that already fetches SPY/URTH).
# Comparing a German stock to the S&P 500 systematically penalises it; a regional
# benchmark removes that bias. Missing benchmark data fails open (signal skipped).
_BENCHMARK_EUROPE = "VGK"  # Vanguard FTSE Developed Europe (USD)
_BENCHMARK_US = "SPY"
_BENCHMARK_GLOBAL_ETF = "URTH"  # iShares MSCI World
_BENCHMARK_JAPAN = "EWJ"  # iShares MSCI Japan (USD); Tokyo listings used to get SPY


def _benchmark_symbols() -> list[str]:
    """Benchmark ETFs warmed alongside candidates on every discover run."""
    return [_BENCHMARK_EUROPE, _BENCHMARK_US, _BENCHMARK_GLOBAL_ETF, _BENCHMARK_JAPAN]


def _ordered_unique(symbols: list[str]) -> list[str]:
    """Dedupe symbols case-insensitively, preserving first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for symbol in symbols:
        key = symbol.upper()
        if key not in seen:
            seen.add(key)
            ordered.append(symbol)
    return ordered


def _preingest_one(symbol: str) -> tuple[str, bool]:
    """Ingest 5 years of bar prices for *symbol* using a private DB session.

    Each call opens and closes its own session so it is safe to run in a
    ThreadPoolExecutor alongside other workers.  Returns (symbol, success).
    """
    db = SessionLocal()
    try:
        ingester = DataIngester(db)
        result = ingester.ingest_bar_prices(symbol, days=_PREINGEST_DAYS)
        return symbol, bool(result.get("success"))
    except Exception:
        logger.debug("pre-ingest failed for %s", symbol, exc_info=True)
        return symbol, False
    finally:
        db.close()


def _preingest_candidates(
    symbols: list[str],
    progress: Any | None = None,
) -> dict[str, Any]:
    """Warm the bar-prices cache for *symbols* plus the benchmark ETFs.

    Each worker creates its own SQLAlchemy session — the caller's session is
    never shared.  Results are written to the shared hypertable / bar_prices
    store so that the subsequent sequential stage loop hits warm caches.

    The benchmark ETFs (``_BENCHMARK_EUROPE`` / ``_BENCHMARK_US`` /
    ``_BENCHMARK_GLOBAL_ETF``) are appended after the candidates — ordered-
    unique union, deduped case-insensitively with candidates taking precedence
    — so later stages score against persisted bars instead of live-fetching
    benchmarks and silently inheriting the provider's vintage (audit F10-F12).

    Args:
        symbols: candidate symbols to warm.
        progress: optional callback ``progress(done, total, symbol)`` invoked as
            each symbol finishes warming. This lets the orchestrator surface a
            live counter during the (otherwise opaque) warm-up phase, which can
            take several minutes for a 50+ symbol universe.

    Returns:
        Summary dict ``{"requested": int, "ok": int, "failed": [symbols]}``.
        Benchmark ingest failures show up in ``failed`` but never abort
        candidate processing.
    """
    if not symbols:
        return {"requested": 0, "ok": 0, "failed": []}
    symbols = _ordered_unique([*symbols, *_benchmark_symbols()])
    total = len(symbols)
    logger.info("pre-ingest: warming bar-price cache for %d symbols", total)
    executor = ThreadPoolExecutor(max_workers=_PREINGEST_MAX_WORKERS)
    ok = fail = done = 0
    failed_symbols: list[str] = []
    try:
        futures = {executor.submit(_preingest_one, sym): sym for sym in symbols}
        pending = set(futures.keys())
        timed_out = False
        while pending:
            done_futures, pending = wait(pending, timeout=_PREINGEST_TIMEOUT_S, return_when=FIRST_COMPLETED)
            if not done_futures:
                # A full timeout window elapsed with zero completions: the remaining
                # workers are stuck. Stop here — re-entering wait() would block again
                # for another full window, so one hung ingest could stall the whole
                # run indefinitely. The pending set is drained as failures below.
                timed_out = True
                break
            for future in done_futures:
                symbol = futures[future]
                try:
                    _, success = future.result()
                    if success:
                        ok += 1
                    else:
                        fail += 1
                        failed_symbols.append(symbol)
                except Exception:
                    fail += 1
                    failed_symbols.append(symbol)
                done += 1
                if progress is not None:
                    try:
                        progress(done, total, symbol)
                    except Exception:
                        logger.debug("pre-ingest progress callback failed", exc_info=True)
        # Anything still pending (a stall, or a trailing partial batch) is cancelled
        # and counted as a failure so the run can proceed.
        for future in pending:
            future.cancel()
            symbol = futures[future]
            if timed_out:
                logger.warning("pre-ingest: timed out for %s (%.0fs)", symbol, _PREINGEST_TIMEOUT_S)
            fail += 1
            failed_symbols.append(symbol)
            done += 1
            if progress is not None:
                try:
                    progress(done, total, symbol)
                except Exception:
                    logger.debug("pre-ingest progress callback failed", exc_info=True)
    finally:
        # Never block the discover run on a hung worker: drop queued tasks and
        # return without joining any in-flight thread (it finishes in the
        # background and closes its own session in _preingest_one's finally).
        executor.shutdown(wait=False, cancel_futures=True)
    logger.info("pre-ingest done: %d ok, %d failed", ok, fail)
    return {"requested": total, "ok": ok, "failed": sorted(failed_symbols)}


# ---------------------------------------------------------------------------
# Stage 1 — history ingest
# ---------------------------------------------------------------------------

_HISTORY_REQUEST_DAYS = 365 * 5

# M1 span/length gate constants (audit F4): the old coverage-only check measured
# coverage against *returned* rows, so a gapless-but-shallow series (e.g. 100
# recent bars) passed with fake "100% coverage" and produced silently wrong
# annualized CAGRs downstream (quant_metrics annualises over whatever N exists).
_SPAN_TOLERANCE_DAYS = 10            # weekend/holiday slack around the cutoff
_SHORT_HISTORY_MIN_AGE_YEARS = 2.0   # young-listing policy: flag, never reject
_MIN_LENGTH_RATIO = 0.55             # scaled length floor vs requested window
_MIN_CLOSES_ABSOLUTE = 250           # legacy absolute floor (message shape kept)
_TRADING_DAYS_PER_YEAR = 252
_MIN_COVERAGE = 0.90                 # unchanged legacy threshold
_DATA_GAP_MEDIAN_DAYS = 3            # concern-only: median calendar spacing
_DATA_GAP_MAX_DAYS = 14              # concern-only: single contiguous hole


def _row_date(value: Any) -> date:
    """Coerce a row date (date, datetime/Timestamp or ISO string) to a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _evaluate_history_rows(
    rows: list[dict[str, Any]],
    requested_days: int,
) -> tuple[dict[str, Any], None] | tuple[None, str]:
    """Shared span/length/coverage gate for cached and freshly ingested history.

    Audit F4: coverage measured against *returned* rows alone let a shallow,
    gapless series score "100% coverage" and flow into annualisation with a
    tiny N (silently wrong CAGRs). This evaluator enforces, in order:

    1. coverage — >= ``_MIN_COVERAGE`` non-null closes (unchanged legacy rule);
    2. span — first bar must reach back to ``today - requested_days`` plus
       ``_SPAN_TOLERANCE_DAYS`` of weekend/holiday slack;
    3. length — ``n_closes >= max(_MIN_CLOSES_ABSOLUTE, ceil(_MIN_LENGTH_RATIO
       * _TRADING_DAYS_PER_YEAR * requested_days / 365))`` (694 for 5y). This
       catches sparse vendor data whose first date claims an old anchor but
       whose bar count cannot support the window;
    4. young listings (audit F4 policy decision, LOCKED): a series too young
       to cover the window but at least ~``_SHORT_HISTORY_MIN_AGE_YEARS`` old
       with >= 250 closes PASSES with a ``short_history`` concern — flagged
       for the LLM/UI, never rejected;
    5. ``data_gap`` (concern-only, never rejects): median calendar spacing
       above ``_DATA_GAP_MEDIAN_DAYS`` days or any single contiguous hole
       beyond ``_DATA_GAP_MAX_DAYS`` days signals gaps the coverage ratio can
       miss when the row set itself is thin.

    Returns ``(scores, None)`` on pass — ``scores["concerns"]`` lists advisory
    flags (empty when clean) — or ``(None, reject_reason)`` where the reason
    keeps the machine-parseable ``"history_ingest: ..."`` prefix.

    Precondition: *rows* is non-empty and ordered by date ascending.
    """
    closes = [r["close"] for r in rows if r.get("close") is not None]
    n_closes = len(closes)
    total_slots = len(rows)
    nan_gaps = sum(1 for r in rows if r.get("close") is None)
    coverage = 1.0 - (nan_gaps / total_slots) if total_slots else 0.0

    first_date = _row_date(rows[0]["date"])
    last_date = _row_date(rows[-1]["date"])
    today = datetime.now(UTC).date()
    cutoff = today - timedelta(days=requested_days)

    if coverage < _MIN_COVERAGE:
        return None, f"history_ingest: coverage {coverage:.1%} (< 90%)"

    length_floor = max(
        _MIN_CLOSES_ABSOLUTE,
        math.ceil(_MIN_LENGTH_RATIO * _TRADING_DAYS_PER_YEAR * (requested_days / 365)),
    )
    span_ok = first_date <= cutoff + timedelta(days=_SPAN_TOLERANCE_DAYS)
    concerns: list[str] = []

    if span_ok:
        if n_closes < _MIN_CLOSES_ABSOLUTE:
            return None, f"history_ingest: only {n_closes} closes (< {_MIN_CLOSES_ABSOLUTE})"
        if n_closes < length_floor:
            return None, f"history_ingest: insufficient length ({n_closes} closes < {length_floor})"
    else:
        age_years = (today - first_date).days / 365.25
        young_listing = age_years >= _SHORT_HISTORY_MIN_AGE_YEARS and n_closes >= _MIN_CLOSES_ABSOLUTE
        if not young_listing:
            return None, f"history_ingest: insufficient span (first {first_date}, need >= {cutoff})"
        # Young-listing policy (LOCKED): flag, never reject.
        concerns.append("short_history")

    # data_gap: concern-only signal (never rejects). Median spacing catches
    # uniformly sparse vendors; the max-hole rule catches single outages.
    dates = [_row_date(r["date"]) for r in rows]
    gaps = [(later - earlier).days for earlier, later in zip(dates, dates[1:])]
    median_gap = statistics.median(gaps) if gaps else 0.0
    if median_gap > _DATA_GAP_MEDIAN_DAYS or (gaps and max(gaps) > _DATA_GAP_MAX_DAYS):
        concerns.append("data_gap")

    scores: dict[str, Any] = {
        "bars": n_closes,
        "coverage": round(coverage, 4),
        "first_date": str(first_date),
        "last_date": str(last_date),
        "concerns": concerns,
    }
    return scores, None


def stage_history_ingest(
    db: Session,
    symbol: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Ingest 5 years of bar prices and validate span/length/coverage.

    Checks DB cache first (populated by warm-up phase) to avoid hitting
    providers when data already exists. Both the cached rows and the freshly
    ingested rows go through the SAME :func:`_evaluate_history_rows` gate, so
    a shallow cache can never bless data the ingest path would reject.
    """
    logger.debug("stage_history_ingest: %s", symbol)

    # Cache-first: check if warm-up already populated the DB for this symbol.
    # On gate failure we deliberately FALL THROUGH to a fresh ingest: the cache
    # may be partial and a provider fetch may repair it.
    rows = market_history(db, symbol, days=_HISTORY_REQUEST_DAYS)
    if rows:
        cached_scores, _cache_err = _evaluate_history_rows(rows, _HISTORY_REQUEST_DAYS)
        if cached_scores is not None:
            logger.debug(
                "stage_history_ingest: %s passed (cached, %d bars)", symbol, cached_scores["bars"],
            )
            return cached_scores, None

    # Fall through: call provider chain via ingester
    ingester = DataIngester(db)
    result = ingester.ingest_bar_prices(symbol, days=_HISTORY_REQUEST_DAYS)

    if not result.get("success"):
        # Provider failed, but whatever the DB still holds must satisfy the
        # identical gate — an outage must not silently bless shallow cache
        # rows either, and the gate's reason string stays machine-parseable.
        fallback_rows = market_history(db, symbol, days=_HISTORY_REQUEST_DAYS)
        if fallback_rows:
            scores, err = _evaluate_history_rows(fallback_rows, _HISTORY_REQUEST_DAYS)
            if scores is not None:
                return scores, None
            return None, err
        return None, f"history_ingest: {result.get('message', 'unknown error')}"

    # Fetch back from DB / cache to count actual closes
    rows = market_history(db, symbol, days=_HISTORY_REQUEST_DAYS)
    if not rows:
        return None, "history_ingest: no rows after ingestion"

    scores, err = _evaluate_history_rows(rows, _HISTORY_REQUEST_DAYS)
    if scores is None:
        return None, err
    logger.debug("stage_history_ingest: %s passed (%d bars)", symbol, scores["bars"])
    return scores, None


# ---------------------------------------------------------------------------
# Stage 1a — AlphaCrafter miner (forward-looking predictive power)
# ---------------------------------------------------------------------------

# A candidate's factor z-score needs a cross-section to be a z-score.
_MIN_EXPOSURE_PANEL = 30
# momentum_12_1 needs 252 bars; this leaves room for holidays and gaps.
_EXPOSURE_LOOKBACK_DAYS = 420
# Cap on one factor's z-score so a single outlier cannot decide the signal.
_EXPOSURE_Z_CLIP = 3.0
# A candidate whose last usable value is older than this (relative to the
# panel's last date) is not scored: a suspended or delisted name.
_EXPOSURE_MAX_AGE_DAYS = 7

# The miner universe's panel and each factor's values on it, built once per
# day and shared by the ~300 candidates of a run; a candidate outside the
# universe adds only its own column.
_EXPOSURE_CACHE: dict[str, Any] = {}
_EXPOSURE_LOCK = threading.Lock()


def _library_factors(db: Session) -> list[dict[str, Any]]:
    """Active FactorsLibrary factors with the IC/ICIR they were validated at.

    Rows without a numeric IC and ICIR (Obsidian-synced hypotheses store
    prose there) are not validated factors and are skipped.
    """
    from sqlalchemy import select

    from app.foundation.models.entities import FactorsLibrary

    factors: list[dict[str, Any]] = []
    for row in db.execute(select(FactorsLibrary).where(FactorsLibrary.retired_at.is_(None))).scalars():
        try:
            formula = json.loads(row.formula_json) if row.formula_json else {}
            summary = json.loads(row.ic_summary_json) if row.ic_summary_json else {}
        except ValueError:
            continue
        if not isinstance(formula, dict) or not isinstance(summary, dict):
            continue
        ic, icir = _safe_float(summary.get("ic")), _safe_float(summary.get("icir"))
        if ic is None or icir is None or ic == 0.0:
            continue
        factors.append({"name": row.name, "dsl": formula.get("dsl") or None, "ic": ic, "icir": icir})
    return factors


def _exposure_base(db: Session) -> dict[str, Any] | None:
    """Today's miner-universe panel and window, built on first use."""
    from app.lab.alphacrafter.universe import MINER_UNIVERSE

    if alpha_build_panel is None:
        return None
    end = datetime.now(UTC)
    today = end.date()
    with _EXPOSURE_LOCK:
        if _EXPOSURE_CACHE.get("day") != today:
            start = end - timedelta(days=_EXPOSURE_LOOKBACK_DAYS)
            _EXPOSURE_CACHE.clear()
            _EXPOSURE_CACHE.update(
                day=today, start=start, end=end, values={},
                panel=alpha_build_panel(db, MINER_UNIVERSE, start, end, include_fundamentals=False),
            )
        panel = _EXPOSURE_CACHE["panel"]
        if not panel or "close" not in panel:
            return None
        return _EXPOSURE_CACHE


def _base_factor_values(base: dict[str, Any], factor: dict[str, Any]) -> pd.DataFrame:
    """*factor*'s values on the universe panel, computed once per day."""
    if alpha_miner_module is None:
        raise RuntimeError("AlphaCrafter miner unavailable")
    key = (factor["name"], factor["dsl"])
    with _EXPOSURE_LOCK:
        cached = base["values"].get(key)
    if cached is None:
        cached = alpha_miner_module.factor_values(
            base["panel"], name=None if factor["dsl"] else factor["name"], dsl=factor["dsl"],
        )
        with _EXPOSURE_LOCK:
            base["values"][key] = cached
    return cached


def _candidate_factor_values(
    base: dict[str, Any], own: dict[str, pd.DataFrame] | None, factor: dict[str, Any], symbol: str,
) -> pd.DataFrame:
    """*factor*'s values across the universe plus *symbol*.

    A seed factor is a per-symbol time series, so the candidate's column is
    computed on its own. A DSL factor may be cross-sectional (``rank``,
    ``zscore``), so it is evaluated on the merged panel.
    """
    values = _base_factor_values(base, factor)
    if symbol in values.columns or own is None or alpha_miner_module is None:
        return values
    if factor["dsl"]:
        merged = {
            key: pd.concat([frame, own[key]], axis=1) if key in own else frame
            for key, frame in base["panel"].items()
        }
        return alpha_miner_module.factor_values(merged, dsl=factor["dsl"])
    extra = alpha_miner_module.factor_values(own, name=factor["name"])
    return pd.concat([values, extra], axis=1)


def stage_alpha_miner(
    db: Session,
    symbol: str,
    *,
    run_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """How strongly *symbol* loads on the AlphaCrafter factors that were validated.

    Grinold & Kahn's alpha is IC x volatility x score: a factor's validated
    information coefficient says whether it predicts, the candidate's
    cross-sectional z-score on it says how much of that prediction applies to
    this name. For every active FactorsLibrary factor this computes the
    candidate's z-score within the miner universe (Euro Stoxx 50 + S&P 100)
    on the latest date, and combines them as
    ``S = sum_f w_f * sign(IC_f) * z_f`` with ``w_f`` proportional to
    ``|ICIR_f|``. ``exposure_score = Phi(S / sqrt(sum w_f^2))`` is the
    percentile of that exposure under independent unit-variance z-scores.

    Until 2026-09-28 this stage measured each factor's IC across the
    candidate and three benchmark ETFs. That is a property of the factor,
    not of the candidate, and a four-point rank correlation is noise; the
    signal has been gated off since (composite.ic_signal_usable).

    ``run_id`` is accepted for the orchestrator's call signature; the stage
    evaluates no new factors, so it writes no trial-ledger rows.

    Returns:
        dict with ``exposure_score`` (0-1, None without validated factors or
        a cross-section), ``ic``/``icir`` (|ICIR|-weighted library values,
        for display), ``n_factors``, ``panel_size``, ``factors`` (per-factor
        ``{name, ic, icir, z}``) and ``concerns``.
    """
    logger.debug("stage_alpha_miner: %s", symbol)
    neutral: dict[str, Any] = {
        "exposure_score": None,
        "ic": 0.0,
        "icir": 0.0,
        "n_factors": 0,
        "panel_size": 0,
        "concerns": ["alphacrafter_miner_unavailable"],
    }

    if not _ALPHACRAFTER_AVAILABLE or alpha_miner_module is None or alpha_build_panel is None:
        logger.info("stage_alpha_miner: AlphaCrafter unavailable, returning neutral for %s", symbol)
        return neutral, None

    try:
        factors = _library_factors(db)
        if not factors:
            return {**neutral, "concerns": ["alphacrafter_no_validated_factors"]}, None

        base = _exposure_base(db)
        if base is None:
            return {**neutral, "concerns": ["alphacrafter_miner_insufficient_panel"]}, None
        own: dict[str, pd.DataFrame] | None = None
        if symbol not in base["panel"]["close"].columns:
            own = alpha_build_panel(db, [symbol], base["start"], base["end"], include_fundamentals=False)
            if not own or "close" not in own or symbol not in own["close"].columns:
                return {**neutral, "concerns": ["alphacrafter_miner_insufficient_panel"]}, None

        rows: list[dict[str, Any]] = []
        panel_size = 0
        for f in factors:
            try:
                values = _candidate_factor_values(base, own, f, symbol)
            except Exception as exc:
                logger.debug("stage_alpha_miner: factor %s failed for %s: %s", f["name"], symbol, exc)
                continue
            if symbol not in values.columns:
                continue
            # The latest date with both the candidate's value and a full
            # cross-section: exchange calendars differ, and a stale value is
            # not today's exposure.
            usable = values[symbol].notna() & (values.notna().sum(axis=1) >= _MIN_EXPOSURE_PANEL)
            if not usable.any():
                continue
            as_of = values.index[usable.to_numpy()][-1]
            if (values.index[-1] - as_of).days > _EXPOSURE_MAX_AGE_DAYS:
                continue
            cross = values.loc[as_of].dropna()
            std = float(cross.std())
            if not math.isfinite(std) or std <= 0:
                continue
            z = (float(cross[symbol]) - float(cross.mean())) / std
            z = max(-_EXPOSURE_Z_CLIP, min(_EXPOSURE_Z_CLIP, z))
            rows.append({**f, "z": round(z, 4)})
            panel_size = max(panel_size, len(cross))

        if not rows:
            return {**neutral, "panel_size": panel_size, "concerns": ["alphacrafter_miner_insufficient_panel"]}, None

        total = sum(abs(r["icir"]) for r in rows)
        weights = [abs(r["icir"]) / total for r in rows] if total > 0 else [1.0 / len(rows)] * len(rows)
        s = sum(w * math.copysign(1.0, r["ic"]) * r["z"] for w, r in zip(weights, rows))
        scale = math.sqrt(sum(w * w for w in weights))
        exposure = 0.5 * (1.0 + math.erf(s / scale / math.sqrt(2.0)))

        return {
            "exposure_score": round(exposure, 4),
            "ic": round(sum(w * r["ic"] for w, r in zip(weights, rows)), 4),
            "icir": round(sum(w * r["icir"] for w, r in zip(weights, rows)), 4),
            "n_factors": len(rows),
            "panel_size": panel_size,
            "factors": [{k: r[k] for k in ("name", "ic", "icir", "z")} for r in rows],
            "concerns": [],
        }, None
    except Exception as exc:
        logger.info("stage_alpha_miner: failed for %s: %s", symbol, exc)
        return {**neutral, "concerns": [f"alphacrafter_miner_error: {exc}"]}, None


# ---------------------------------------------------------------------------
# Stage 1b — AlphaCrafter screener (regime-conditioned factor selection)
# ---------------------------------------------------------------------------

def stage_alpha_screener(
    db: Session,
    symbol: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Compute regime-conditioned factor affinity using AlphaCrafter screener.

    Uses the current macro-regime label and crisis flag to score how well the
    candidate's typical factor exposures align with the regime. Fail-open with
    neutral scores when AlphaCrafter is unavailable.
    """
    logger.debug("stage_alpha_screener: %s", symbol)
    neutral: dict[str, Any] = {
        "regime_affinity": 0.5,
        "correlation_score": 0.5,
        "concerns": ["alphacrafter_screener_unavailable"],
    }

    if not _ALPHACRAFTER_AVAILABLE or alpha_screener_module is None:
        logger.info("stage_alpha_screener: AlphaCrafter unavailable, returning neutral for %s", symbol)
        return neutral, None

    try:
        regime_gate = MacroRegimeGate(db)
        ctx = regime_gate.context_dict()
        label = ctx.get("regime_label")
        crisis = bool(ctx.get("crisis", False))

        categories = ["momentum", "quality", "value", "size", "low_vol"]
        affinities = [
            alpha_screener_module.regime_affinity(cat, label, crisis)
            for cat in categories
        ]
        regime_affinity_score = float(sum(affinities) / len(affinities)) if affinities else 0.5
        correlation_score = 0.5

        return {
            "regime_affinity": round(max(0.0, min(1.0, regime_affinity_score)), 4),
            "correlation_score": round(correlation_score, 4),
            "regime_label": label,
            "crisis": crisis,
            "concerns": [],
        }, None
    except Exception as exc:
        logger.info("stage_alpha_screener: failed for %s: %s", symbol, exc)
        return {**neutral, "concerns": [f"alphacrafter_screener_error: {exc}"]}, None


# ---------------------------------------------------------------------------
# Stage 1c — sentiment + fundamentals
# ---------------------------------------------------------------------------

def _apply_analyst_consensus(result: dict[str, Any], est_result: dict[str, Any]) -> None:
    """Fill the ``analyst_*`` fields of *result* from a provider consensus payload.

    Prefers the median price target, then the mean. The median is robust to
    a single outlier analyst: HFG.DE's mean target was 4.82 against a median
    of 3.50 (prod, 2026-09-26). ``target_high`` is never used, because it is
    the single most bullish analyst's number. With no usable target a
    buy/sell label still maps to 0.75/0.25; a bare "hold" maps to 0.5, since
    "hold" is a real reading, not a missing one.
    """
    est = est_result.get("data") or {}
    # A provider capability bug (wrong endpoint returning a list of
    # trend/period rows instead of a single consensus dict) has twice
    # produced a non-dict payload here — guard rather than trust every
    # provider's shape unconditionally.
    if not isinstance(est, dict):
        return
    target = _safe_float(
        est.get("target_median")
        or est.get("target_consensus")
        or est.get("target_mean")
    )
    current = _safe_float(est.get("current_price"))
    rec = str(est.get("recommendation") or "").lower() or None
    count = est.get("number_analysts")
    result["analyst_recommendation"] = rec
    result["analyst_count"] = int(count) if isinstance(count, (int, float)) else None
    result["analyst_source"] = est_result.get("provider")
    for key in ("sector", "industry"):
        value = est.get(key)
        if result.get(key) is None and isinstance(value, str) and value.strip():
            result[key] = value
    if target is not None and target > 0 and current is not None and current > 0:
        upside = (target - current) / current
        result["analyst_target"] = round(target, 4)
        result["analyst_upside"] = round(upside, 4)
        result["analyst_estimate_score"] = round(max(0.0, min(1.0, 0.5 + upside * 2.0)), 4)
    elif rec is not None:
        if "buy" in rec:
            result["analyst_estimate_score"] = 0.75
        elif "sell" in rec:
            result["analyst_estimate_score"] = 0.25
        elif "hold" in rec:
            result["analyst_estimate_score"] = 0.5


# A report this close is event risk the dossier must name: the price move on
# the day is typically several times a normal day's. It is not a bearish
# signal — announcement months earn a premium on average (Frazzini & Lamont
# 2007; Savor & Wilson 2016) — so it is flagged, never scored.
_EARNINGS_WARN_DAYS = 14


def attach_earnings_calendar(db: Session, symbol: str, scores: dict[str, Any]) -> dict[str, Any]:
    """Add the next earnings report to a shortlisted candidate's *scores*.

    MU reported two days after the 2026-09-28 run and nothing in its dossier
    said so. Called for the shortlist only (one Yahoo request per name, not
    per screened candidate). Writes ``next_earnings_date``,
    ``earnings_date_to``, ``earnings_hour`` and ``days_to_earnings`` into
    ``scores["sentiment_fundamentals"]`` and appends an
    ``"earnings on <date> (in <n> days)"`` concern when the report is within
    _EARNINGS_WARN_DAYS. Fail-open: without a calendar *scores* is unchanged.
    """
    try:
        cal = build_provider_registry(db).get_earnings_calendar(symbol)
    except Exception as exc:
        logger.debug("attach_earnings_calendar: lookup failed for %s: %s", symbol, exc)
        return scores
    data = cal.get("data") if cal.get("ok") else None
    if not isinstance(data, dict) or not data.get("next_earnings_date"):
        return scores
    try:
        report_day = datetime.fromisoformat(str(data["next_earnings_date"])).date()
    except ValueError:
        return scores
    days = (report_day - datetime.now(UTC).date()).days
    sf = scores.get("sentiment_fundamentals")
    sf = dict(sf) if isinstance(sf, dict) else {}
    sf.update({
        "next_earnings_date": report_day.isoformat(),
        "earnings_date_to": data.get("earnings_date_to"),
        "earnings_hour": data.get("hour"),
        "days_to_earnings": days,
    })
    scores["sentiment_fundamentals"] = sf
    if 0 <= days <= _EARNINGS_WARN_DAYS:
        concerns = [c for c in scores.get("concerns") or [] if not str(c).startswith("earnings on ")]
        concerns.insert(0, f"earnings on {report_day.isoformat()} (in {days} days)")
        scores["concerns"] = concerns
    return scores


def stage_sentiment_fundamentals(
    db: Session,
    symbol: str,
    is_etf: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch fundamentals and recent news sentiment for *symbol*.

    Uses the provider registry for fundamentals and the NewsItem table for
    sentiment. Returns simple 0-1 scores plus raw metrics. Fail-open on any
    provider/DB failure.

    ``analyst_estimate_score`` stays ``None`` when no analyst consensus could
    be fetched, and a single-name equity then carries an
    ``analyst_unavailable`` concern. It used to default to a neutral 0.5 that
    the composite weighted in as if it were real data: during the 2026-09-25
    run the OpenBB guardrail throttled 127 of 271 candidates this way,
    including BBVA.MC, whose real consensus target sat 8% below the price.
    Funds have no analyst coverage by construction, so ``is_etf`` skips the
    lookup and the concern.
    """
    logger.debug("stage_sentiment_fundamentals: %s", symbol)
    from app.foundation.models.entities import NewsItem

    result: dict[str, Any] = {
        "sentiment_score": 0.5,
        "fundamentals_score": 0.5,
        "analyst_estimate_score": None,
        "analyst_target": None,
        "analyst_upside": None,
        "analyst_count": None,
        "analyst_recommendation": None,
        "analyst_source": None,
        "sector": None,
        "industry": None,
        "pe": None,
        "pb": None,
        "roe": None,
        "de_ratio": None,
        "market_cap": None,
        "revenue_growth": None,
        "social_mentions": 0,
        "concerns": [],
    }

    try:
        from app.foundation.market import fundamentals as market_fundamentals

        fund_result = market_fundamentals(db, symbol)
        data = fund_result.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        pe = _safe_float(data.get("pe_ratio"))
        pb = _safe_float(data.get("pb_ratio"))
        roe = _safe_float(data.get("roe"))
        de = _safe_float(data.get("debt_equity"))
        mc = _safe_float(data.get("market_cap"))
        rg = _safe_float(data.get("revenue_growth"))

        result["pe"] = pe
        result["pb"] = pb
        result["roe"] = roe
        result["de_ratio"] = de
        result["market_cap"] = mc
        result["revenue_growth"] = rg
        # Sector/industry feed the shortlist's per-sector cap
        # (apply_sector_cap) and the dossier prompt.
        for key in ("sector", "industry"):
            value = data.get(key)
            result[key] = value if isinstance(value, str) and value.strip() else None

        score = 0.5
        # A non-positive P/E or P/B is a loss or negative equity, not a cheap
        # stock; the terms below would score it as the cheapest possible.
        if pe is not None and pe > 0:
            score += 0.1 * max(-0.5, min(0.5, (15.0 - pe) / 30.0))
        if pb is not None and pb > 0:
            score += 0.1 * max(-0.5, min(0.5, (1.5 - pb) / 3.0))
        if roe is not None:
            score += 0.1 * max(-0.5, min(0.5, roe / 0.3))
        if de is not None:
            score -= 0.1 * max(-0.5, min(0.5, de / 200.0))
        if rg is not None:
            score += 0.1 * max(-0.5, min(0.5, rg / 0.5))
        result["fundamentals_score"] = round(max(0.0, min(1.0, score)), 4)
    except Exception as exc:
        logger.debug("stage_sentiment_fundamentals: fundamentals failed for %s: %s", symbol, exc)
        result["concerns"].append("fundamentals_unavailable")

    if not is_etf:
        try:
            registry = build_provider_registry(db)
            est_result = registry.get_analyst_estimates(symbol)
            if est_result.get("ok"):
                _apply_analyst_consensus(result, est_result)
        except Exception as exc:
            logger.debug("stage_sentiment_fundamentals: analyst estimates failed for %s: %s", symbol, exc)
        if result["analyst_estimate_score"] is None:
            result["concerns"].append("analyst_unavailable")

    try:
        since = datetime.now(UTC) - timedelta(days=30)
        rows = (
            db.query(NewsItem)
            .filter(NewsItem.ticker == symbol.upper(), NewsItem.published_at >= since)
            .order_by(NewsItem.published_at.desc())
            .limit(50)
            .all()
        )
        if rows:
            result["social_mentions"] = len(rows)
            scores = [float(r.sentiment_score) for r in rows if r.sentiment_score is not None]
            if scores:
                avg = sum(scores) / len(scores)
                if avg < 0:
                    result["sentiment_score"] = round(max(0.0, min(1.0, (avg + 1.0) / 2.0)), 4)
                else:
                    result["sentiment_score"] = round(max(0.0, min(1.0, avg)), 4)
    except Exception as exc:
        logger.debug("stage_sentiment_fundamentals: sentiment query failed for %s: %s", symbol, exc)
        result["concerns"].append("sentiment_unavailable")

    return result, None


# ---------------------------------------------------------------------------
# Stage 2 — momentum quality (backward-looking)
# ---------------------------------------------------------------------------

def stage_momentum_quality(
    db: Session,
    symbol: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Compute 12-1m momentum, 6m volatility, max drawdown.

    Quality breaches (weak momentum / high vol) are recorded as advisory
    ``concerns`` rather than rejecting — the LLM agent weighs them. Only the
    genuine inability to compute (no price data) is a hard failure.
    """
    logger.debug("stage_momentum_quality: %s", symbol)
    rows = market_history(db, symbol, days=400)
    if not rows:
        return None, "momentum_quality: no price data"

    df = pd.DataFrame(rows)
    if "close" not in df.columns:
        return None, "momentum_quality: no close prices"

    df = df.dropna(subset=["close"]).sort_values("date")
    if len(df) < 130:
        return None, f"momentum_quality: only {len(df)} rows (< 130)"

    # 12-1 month momentum: return from 252 trading days ago to 21 trading days
    # ago, skipping the most recent month to strip short-term reversal (the
    # standard Jegadeesh-Titman convention — matches quant_factors.momentum_12_1).
    if len(df) >= 252:
        prev_close = df["close"].iloc[-252]
    else:
        prev_close = df["close"].iloc[0]
    if prev_close == 0:
        return None, "momentum_quality: zero prior close"
    skip_close = df["close"].iloc[-21] if len(df) >= 21 else df["close"].iloc[-1]
    momentum = (skip_close / prev_close) - 1.0

    # 6-month volatility (approx 126 trading days)
    window = min(126, len(df) - 1)
    recent = df["close"].iloc[-window:]
    returns = recent.pct_change().dropna().tolist()
    vol = annualised_volatility(returns) if len(returns) >= 2 else 0.0

    # Max drawdown on full series
    full_returns = df["close"].pct_change().dropna().tolist()
    mdd_info = max_drawdown(full_returns)
    mdd = mdd_info.get("max_drawdown", 0.0)

    # Trailing ~1-month annualised return (last 21 trading days). Too noisy
    # as a forward-return proxy for most instruments, but for a money-market
    # / overnight-rate tracker it converges to the current policy rate almost
    # immediately (no duration risk) — unlike a 3y trailing CAGR, which can
    # span several rate regimes. Computed unconditionally here since the
    # price data is already loaded; only money-market candidates actually
    # consume it as their return anchor (see dossier_writer.py).
    trailing_1m_window = min(21, len(df) - 1)
    trailing_1m_returns = df["close"].iloc[-(trailing_1m_window + 1) :].pct_change().dropna().tolist()
    trailing_1m_annualized_return = annualised_return(trailing_1m_returns) if trailing_1m_returns else 0.0

    concerns: list[str] = []
    if momentum < _MOMENTUM_MIN:
        concerns.append(f"weak 12m momentum {momentum:.1%}")
    if vol > _VOL_MAX:
        concerns.append(f"elevated 6m volatility {vol:.1%}")

    scores = {
        "momentum_12_1m": round(float(momentum), 4),
        "volatility_6m": round(vol, 4),
        "max_drawdown": round(mdd, 4),
        "trailing_1m_annualized_return": round(float(trailing_1m_annualized_return), 4),
        "concerns": concerns,
    }
    logger.debug("stage_momentum_quality: %s mom=%.2f vol=%.2f concerns=%d", symbol, momentum, vol, len(concerns))
    return scores, None


# ---------------------------------------------------------------------------
# Stage 3 — backtest vs benchmark
# ---------------------------------------------------------------------------

def _pick_benchmark(symbol: str, is_etf: bool) -> str:
    """Choose a region-appropriate benchmark for *symbol*.

    ETFs are global → MSCI World. Otherwise the listing suffix decides: European
    venues benchmark against developed Europe rather than the S&P 500.
    """
    if is_etf:
        return _BENCHMARK_GLOBAL_ETF
    s = symbol.upper()
    if is_eu_listing(s):
        return _BENCHMARK_EUROPE
    region = listing_region(s)
    if region == "japan":
        return _BENCHMARK_JAPAN
    if region != "us":
        return _BENCHMARK_GLOBAL_ETF
    return _BENCHMARK_US


def _infer_currency(db: Session | None, symbol: str) -> str:
    """ISO currency of *symbol*'s stored prices (pence -> GBP).

    The provider-reported quote currency when one is recorded, else the
    listing suffix. The suffix alone read IWDA.L and CSPX.L (USD lines on
    the LSE) as GBP, so their benchmark leg was FX-adjusted by GBPUSD.
    """
    return resolve_currency(db, symbol)


# SPY/VGK/URTH are all US-listed and settle in USD, so this is the only
# direction the FX adjustment below needs to run in. Cached per calendar day:
# the FX series is stable across the ~300 candidates of one discover run, and
# re-fetching per candidate would be 100+ redundant calls for the same pair.
# The day is part of the key because the worker process lives for weeks; a
# process-lifetime cache served the first run's series to every later run.
_FX_SERIES_CACHE: dict[tuple[str, date], "pd.DataFrame | None"] = {}


def _get_fx_series(db: Session, currency: str) -> "pd.DataFrame | None":
    """Daily USD->*currency* rate series (e.g. ``USDEUR=X``), or ``None`` if unavailable."""
    if currency == "USD":
        return None
    fx_symbol = f"USD{currency}=X"
    key = (fx_symbol, date.today())
    if key in _FX_SERIES_CACHE:
        return _FX_SERIES_CACHE[key]
    df: pd.DataFrame | None = None
    try:
        # Nothing else re-ingests FX pairs: without this the stored series
        # froze (USDEUR=X at 2026-09-01) and was forward-filled for weeks.
        refresh_stale_bars(db, fx_symbol)
        rows = market_history(db, fx_symbol, days=365 * 3 + 30)
        if rows:
            df = pd.DataFrame(rows).dropna(subset=["close"]).sort_values("date")
            df = df.assign(date=pd.to_datetime(df["date"]))
    except Exception:
        logger.debug("_get_fx_series: failed to fetch %s", fx_symbol, exc_info=True)
    _FX_SERIES_CACHE[key] = df
    return df


def _fx_adjust_bench_returns(
    db: Session,
    candidate_currency: str,
    bench_currency: str,
    dates: pd.Series,
    bench_rets: pd.Series,
) -> pd.Series | None:
    """Restate *bench_rets* (in ``bench_currency``) into ``candidate_currency`` terms.

    Comparing a EUR-priced stock's returns directly against a USD-listed
    benchmark's unconverted USD returns bakes the EURUSD move into the excess
    return and beta — not a real performance difference. Restated return is
    ``(1+r_local)*(1+r_fx) - 1`` where ``r_fx`` is the daily return of the
    USD->candidate_currency rate. Returns ``None`` (caller keeps the
    unconverted series) when the currencies already match or the FX series
    can't be fetched — fails open like every other stage here.
    """
    if candidate_currency == bench_currency:
        return None
    fx_df = _get_fx_series(db, candidate_currency)
    if fx_df is None or fx_df.empty:
        return None
    fx_series = fx_df.set_index("date")["close"]
    aligned = fx_series.reindex(dates).ffill()
    if bool(aligned.isna().any()):
        return None
    fx_rets = aligned.pct_change()
    # First element of bench_rets/fx_rets has no prior day; align by dropping it.
    adjusted = (1.0 + bench_rets.reset_index(drop=True)) * (1.0 + fx_rets.reset_index(drop=True)) - 1.0
    return adjusted.dropna()


def stage_backtest_vs_benchmark(
    db: Session,
    symbol: str,
    is_etf: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """3-year backtest vs a region-appropriate benchmark.

    Computes excess return and Sharpe delta. Data-availability problems (missing
    benchmark, too few aligned observations) fail open to a neutral signal rather
    than rejecting — a benchmark we cannot fetch is not the candidate's fault.
    Under-performance is recorded as an advisory ``concern``, not a rejection.
    """
    logger.debug("stage_backtest_vs_benchmark: %s", symbol)
    benchmark = _pick_benchmark(symbol, is_etf)

    def _neutral(reason: str) -> tuple[dict[str, Any], None]:
        return {
            "excess_return_annual": None,
            "candidate_return_annual": None,
            "sharpe_delta": None,
            "benchmark": benchmark,
            "note": reason,
            "concerns": [],
        }, None

    cand_rows = market_history(db, symbol, days=365 * 3 + 30)
    bench_rows = market_history(db, benchmark, days=365 * 3 + 30)

    if not cand_rows or not bench_rows:
        return _neutral("benchmark or candidate price data unavailable")

    # Audit F16: a frozen series used to be served as fresh, silently
    # truncating anchors. Surface vintage problems as advisory concerns.
    stale_concerns: list[str] = []
    for leg_symbol, leg_rows in ((symbol, cand_rows), (benchmark, bench_rows)):
        if any(r.get("stale") for r in leg_rows):
            stale_concerns.append(f"{leg_symbol} data stale since {leg_rows[-1]['date']}")

    cand_df = pd.DataFrame(cand_rows).dropna(subset=["close"]).sort_values("date")
    bench_df = pd.DataFrame(bench_rows).dropna(subset=["close"]).sort_values("date")

    if len(cand_df) < 250 or len(bench_df) < 250:
        return _neutral("too few observations for a 3y comparison")

    # Align on common dates (normalise both to datetime64 so dtype never blocks).
    cand_df = cand_df.assign(date=pd.to_datetime(cand_df["date"]))
    bench_df = bench_df.assign(date=pd.to_datetime(bench_df["date"]))
    merged = pd.merge(cand_df[["date", "close"]], bench_df[["date", "close"]], on="date", suffixes=("_c", "_b"))
    if len(merged) < 250:
        return _neutral("insufficient aligned observations vs benchmark")

    cand_rets_series = merged["close_c"].pct_change()
    bench_rets_series = merged["close_b"].pct_change()

    # Neither series is USD/USD; both the candidate and the benchmark returns
    # above are each in their own listing currency. Restate the benchmark leg
    # into the candidate's currency so excess return isn't really an FX bet.
    fx_adjusted = _fx_adjust_bench_returns(
        db, _infer_currency(db, symbol), "USD",
        cast(pd.Series, merged["date"]), cast(pd.Series, bench_rets_series),
    )
    if fx_adjusted is not None:
        bench_rets_series = fx_adjusted

    cand_rets = cand_rets_series.dropna().tolist()
    bench_rets = bench_rets_series.dropna().tolist()
    if len(cand_rets) != len(bench_rets):
        # FX alignment dropped different rows than the plain dropna above —
        # fall back to the unconverted comparison rather than misalign.
        bench_rets = merged["close_b"].pct_change().dropna().tolist()

    cand_ann = annualised_return(cand_rets)
    bench_ann = annualised_return(bench_rets)
    excess = cand_ann - bench_ann

    cand_sharpe = sharpe_ratio(cand_rets)
    bench_sharpe = sharpe_ratio(bench_rets)
    sharpe_delta = cand_sharpe - bench_sharpe

    # Information ratio and its t-statistic (IR x sqrt(years)): outperformance
    # per unit of tracking error, which composite.py scores instead of raw
    # excess return (ADR 0017). The candidate's own volatility over the same
    # window sizes the expected-return credibility weight (dossier_writer).
    active = [c - b for c, b in zip(cand_rets, bench_rets)]
    tracking_error = annualised_volatility(active) if len(active) >= 2 else 0.0
    years = len(cand_rets) / 252.0
    information_ratio = excess / tracking_error if tracking_error > 0 else 0.0
    active_t_stat = information_ratio * math.sqrt(years)
    cand_vol = annualised_volatility(cand_rets) if len(cand_rets) >= 2 else 0.0

    concerns: list[str] = stale_concerns
    if excess < _EXCESS_MIN:
        concerns.append(f"trails {benchmark} by {abs(excess):.1%}/yr")
    if sharpe_delta < _SHARPE_DELTA_MIN:
        concerns.append(f"Sharpe {sharpe_delta:.2f} below {benchmark}")

    scores = {
        "excess_return_annual": round(excess, 4),
        "candidate_return_annual": round(cand_ann, 4),
        "sharpe_delta": round(sharpe_delta, 4),
        "candidate_sharpe": round(cand_sharpe, 4),
        "benchmark_sharpe": round(bench_sharpe, 4),
        "tracking_error": round(tracking_error, 4),
        "information_ratio": round(information_ratio, 4),
        "active_t_stat": round(active_t_stat, 4),
        "candidate_volatility_annual": round(cand_vol, 4),
        "window_years": round(years, 2),
        "benchmark": benchmark,
        "concerns": concerns,
    }
    logger.debug("stage_backtest_vs_benchmark: %s excess=%.2f sharpe_delta=%.2f concerns=%d", symbol, excess, sharpe_delta, len(concerns))
    return scores, None


# ---------------------------------------------------------------------------
# Stage 4 — verification gate (HARD GATE)
# ---------------------------------------------------------------------------

def stage_verification_gate(
    db: Session,
    symbol: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Verification-style risk assessment on the candidate's own price history.

    Breaches of the conservative risk thresholds are recorded as advisory
    ``concerns`` for the LLM agent rather than rejecting the candidate. Only the
    inability to compute (no data / too few returns) is a hard failure.
    """
    logger.debug("stage_verification_gate: %s", symbol)
    rows = market_history(db, symbol, days=365)
    if not rows:
        return None, "verification_gate: no price data"

    df = pd.DataFrame(rows).dropna(subset=["close"]).sort_values("date")
    if len(df) < 60:
        return None, f"verification_gate: only {len(df)} rows (< 60)"

    rets = df["close"].pct_change().dropna().tolist()
    if len(rets) < 20:
        return None, "verification_gate: insufficient returns"

    vol = annualised_volatility(rets)
    mdd_info = max_drawdown(rets)
    mdd = abs(mdd_info.get("max_drawdown", 0.0))
    sharpe = sharpe_ratio(rets)

    concerns: list[str] = []
    if mdd > _MDD_MAX:
        concerns.append(f"max drawdown {mdd:.1%} exceeds {_MDD_MAX:.0%}")
    if vol > _VOL_MAX:
        concerns.append(f"volatility {vol:.1%} exceeds {_VOL_MAX:.0%}")
    if sharpe < _SHARPE_DELTA_MIN and vol > 0.30:
        concerns.append(f"negative Sharpe {sharpe:.2f} with elevated vol")

    scores = {
        "volatility": round(vol, 4),
        "max_drawdown": round(mdd, 4),
        "sharpe": round(sharpe, 4),
        "concerns": concerns,
    }
    logger.debug("stage_verification_gate: %s vol=%.2f mdd=%.2f concerns=%d", symbol, vol, mdd, len(concerns))
    return scores, None


# ---------------------------------------------------------------------------
# Stage 4b — deterministic quant signals (load-bearing; instrument-type-aware)
# ---------------------------------------------------------------------------


_WEEKLY_BETA_DAYS = 730
_WEEKLY_BETA_MIN_WEEKS = 52

# Five loadings on fewer than ~half a year of daily returns are mostly noise.
_FACTOR_MIN_OBS = 120


def _weekly_beta(db: Session, symbol: str, benchmark: str) -> float | None:
    """Beta from two years of Friday-to-Friday returns vs *benchmark*.

    Daily beta against a US-listed benchmark ETF is biased toward zero for a
    European listing: Madrid/Milan/Frankfurt close hours before New York, so
    half of each day's common move lands on the next day's candidate return
    (Scholes & Williams 1977; Dimson 1979). Weekly returns share almost all
    of their window, which removes most of that bias. BBVA.MC measured 0.85
    daily vs 1.10 weekly against VGK (prod, 2026-09-26). The benchmark is
    restated into the candidate's currency first, like the daily path.
    Returns None (fail-open) on missing data or fewer than
    ``_WEEKLY_BETA_MIN_WEEKS`` aligned weeks.
    """
    cand_rows = market_history(db, symbol, days=_WEEKLY_BETA_DAYS)
    bench_rows = market_history(db, benchmark, days=_WEEKLY_BETA_DAYS)
    if not cand_rows or not bench_rows:
        return None

    def _closes(rows: list[dict[str, Any]]) -> pd.Series:
        df = pd.DataFrame(rows).dropna(subset=["close"]).sort_values("date")
        return cast(pd.Series, df.assign(date=pd.to_datetime(df["date"])).set_index("date")["close"]).astype(float)

    cand = _closes(cand_rows)
    bench = _closes(bench_rows)
    currency = _infer_currency(db, symbol)
    if currency != "USD":
        fx_df = _get_fx_series(db, currency)
        if fx_df is not None and not fx_df.empty:
            fx = fx_df.set_index("date")["close"].astype(float)
            bench = (bench * fx.reindex(bench.index).ffill()).dropna()
    weekly = pd.concat(
        [cand.resample("W-FRI").last(), bench.resample("W-FRI").last()],
        axis=1,
        keys=["c", "b"],
    ).dropna()
    rets = weekly.pct_change().dropna()
    if len(rets) < _WEEKLY_BETA_MIN_WEEKS:
        return None
    return beta_vs_benchmark(rets["c"].tolist(), rets["b"].tolist())


def stage_quant_signals(
    db: Session,
    symbol: str,
    is_etf: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Deterministic quant feature set: VaR/CVaR, market beta, factor betas.

    This stage is load-bearing for the composite ranking (unlike the advisory
    AlphaCrafter stages). Instrument-type-aware: ETFs get market beta / vol /
    VaR only (no single-name Fama-French factor regression); individual stocks
    additionally get factor betas (market/size/value/momentum) when factor
    return data is available. Factor data unavailability fails open (betas
    ``None``) — VaR/CVaR from the candidate's own history is always computed.
    """
    logger.debug("stage_quant_signals: %s", symbol)
    rows = market_history(db, symbol, days=365)
    if not rows:
        return None, "quant_signals: no price data"

    df = pd.DataFrame(rows).dropna(subset=["close"]).sort_values("date")
    if len(df) < 60:
        return None, f"quant_signals: only {len(df)} rows (< 60)"

    ret_series = df["close"].pct_change().iloc[1:]
    rets = ret_series.tolist()
    ret_dates = [str(d)[:10] for d in df["date"].iloc[1:]]
    if len(rets) < 20:
        return None, "quant_signals: insufficient returns"

    # Daily VaR/CVaR at 95%, both as positive loss magnitudes. Must come from
    # quant_metrics' paired historical_var/historical_cvar (both keyed off the
    # same empirical order statistic) rather than an independently computed
    # VaR — CVaR is only guaranteed >= VaR when both share a tail definition.
    var_95 = historical_var(rets, confidence=0.95)
    cvar_95 = historical_cvar(rets, confidence=0.95)

    # Market beta vs region-appropriate benchmark (fail-open to None).
    market_beta: float | None = None
    benchmark = _pick_benchmark(symbol, is_etf)
    try:
        bench_rows = market_history(db, benchmark, days=365)
        if bench_rows:
            bench_df = pd.DataFrame(bench_rows).dropna(subset=["close"]).sort_values("date")
            cand = df.assign(date=pd.to_datetime(df["date"])).set_index("date")["close"]
            bench = bench_df.assign(date=pd.to_datetime(bench_df["date"])).set_index("date")["close"]
            common = cand.index.intersection(bench.index)
            if len(common) >= 60:
                cand_r = cand.loc[common].pct_change()
                bench_r = bench.loc[common].pct_change()
                fx_adjusted = _fx_adjust_bench_returns(
                    db, _infer_currency(db, symbol), "USD",
                    pd.Series(common), bench_r.reset_index(drop=True),
                )
                if fx_adjusted is not None:
                    bench_r = fx_adjusted
                cand_r = cand_r.dropna()
                bench_r = bench_r.dropna()
                n = min(len(cand_r), len(bench_r))
                market_beta = beta_vs_benchmark(cand_r.tolist()[-n:], bench_r.tolist()[-n:])
    except Exception:
        logger.debug("stage_quant_signals: market beta failed for %s", symbol, exc_info=True)

    market_beta_weekly: float | None = None
    try:
        market_beta_weekly = _weekly_beta(db, symbol, benchmark)
    except Exception:
        logger.debug("stage_quant_signals: weekly beta failed for %s", symbol, exc_info=True)

    # Fama-French factor betas — stocks only (single-name factor regression is
    # meaningless for diversified ETFs; they get tracking/vol/VaR instead).
    # The factors come from the stock's own market: a US share regressed on
    # Developed-Europe factors gave MU an RMW loading of -5.3 and SPY a
    # market beta of 0.50 (prod, 2026-09-28; ADR 0007 amendment).
    factor_betas: dict[str, float] | None = None
    factor_fit: dict[str, Any] | None = None
    if not is_etf:
        try:
            from app.foundation.market import usd_per_unit_by_date
            from app.foundation.quant_factors import (
                compute_factor_attribution_by_date,
                get_ff5_returns_for_listing,
                restate_in_usd,
            )

            ff = get_ff5_returns_for_listing(listing_region(symbol))
            # Ken French's factors are USD returns; a local-currency return
            # regressed on them carries the FX move as noise (ADR 0007
            # amendment 2). Fails open to local returns when no rate exists.
            reg_rets, reg_dates = list(rets), list(ret_dates)
            currency = _infer_currency(db, symbol)
            returns_in = currency
            if currency != "USD":
                usd_rates = usd_per_unit_by_date(db, currency)
                if usd_rates:
                    reg_rets, reg_dates = restate_in_usd(
                        rets, ret_dates, usd_rates, first_start=str(df["date"].iloc[0])[:10],
                    )
                    returns_in = "USD"
            else:
                returns_in = "USD"
            if ff.get("status") == "completed" and ff.get("factors"):
                # {factor: {date_str: value}}. Join on date, not tail
                # position: the factor file lags the price history by a
                # month or more, so tail alignment regressed each day's
                # return on a different day's factors.
                factors_by_date = {
                    fname: vals
                    for fname, vals in ff["factors"].items()
                    if isinstance(vals, dict) and vals
                }
                if factors_by_date:
                    attribution = compute_factor_attribution_by_date(reg_rets, reg_dates, factors_by_date)
                    n_obs = int(attribution.get("n_observations") or 0)
                    if attribution.get("status") == "completed" and n_obs >= _FACTOR_MIN_OBS:
                        factor_betas = attribution.get("exposures")
                        factor_fit = {
                            "region": ff.get("region"),
                            "r_squared": round(float(attribution.get("r_squared") or 0.0), 4),
                            "n_obs": n_obs,
                            "returns_in": returns_in,
                        }
        except Exception:
            logger.debug("stage_quant_signals: factor betas failed for %s", symbol, exc_info=True)

    # WRDS/JKP factor characteristics (gvkey-verified, see pit_panel_joins) —
    # a per-symbol directional heuristic, not a cross-sectional z-score
    # (this stage has no universe context to z-score against; that lives in
    # alphacrafter's build_panel). Cheap (high be_me), profitable (high
    # gp_at), and not over-investing (low at_gr1) all nudge positive.
    factor_characteristics_score: float | None = None
    try:
        characteristics = pit_wrds_factor_characteristics_for_symbol(
            db, symbol, pd.DatetimeIndex([pd.Timestamp.now(UTC)])
        )
        if characteristics is not None and not characteristics.empty:
            row = characteristics.iloc[0]
            be_me = row.get("be_me")
            gp_at = row.get("gp_at")
            at_gr1 = row.get("at_gr1")
            if pd.notna(be_me) or pd.notna(gp_at) or pd.notna(at_gr1):
                score = 0.5
                if pd.notna(be_me):
                    score += 0.15 * max(-1.0, min(1.0, (float(be_me) - 0.5) / 0.5))
                if pd.notna(gp_at):
                    score += 0.15 * max(-1.0, min(1.0, (float(gp_at) - 0.3) / 0.3))
                if pd.notna(at_gr1):
                    score -= 0.15 * max(-1.0, min(1.0, float(at_gr1) / 0.2))
                factor_characteristics_score = round(max(0.0, min(1.0, score)), 4)
    except Exception:
        logger.debug("stage_quant_signals: factor characteristics failed for %s", symbol, exc_info=True)

    scores = {
        "instrument_type": "etf" if is_etf else "equity",
        "var_95_daily": round(var_95, 5),
        "cvar_95_daily": round(float(cvar_95), 5),
        "market_beta": round(market_beta, 4) if market_beta is not None else None,
        # The canonical market beta (dossier prompt, expected-return prior);
        # market_beta above is the daily estimate, kept for continuity.
        "market_beta_weekly": round(market_beta_weekly, 4) if market_beta_weekly is not None else None,
        "factor_betas": {k: round(v, 4) for k, v in factor_betas.items()} if factor_betas else None,
        "factor_fit": factor_fit,
        "factor_characteristics_score": factor_characteristics_score,
        "benchmark": benchmark,
        "concerns": [],
    }
    logger.debug(
        "stage_quant_signals: %s var=%.4f cvar=%.4f beta=%s",
        symbol, var_95, cvar_95, market_beta,
    )
    return scores, None


# ---------------------------------------------------------------------------
# Stage 5 — portfolio fit
# ---------------------------------------------------------------------------

_FIT_HISTORY_DAYS = 730
_FIT_MIN_WEEKS = 26


def stage_portfolio_fit(
    db: Session,
    user_id: str,
    symbol: str,
    is_etf: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    """Compute complement-aware portfolio fit score.

    Higher score for low correlation with existing holdings and low ETF overlap.

    The correlation is measured on Friday-to-Friday returns over two years,
    like ``_weekly_beta``. Daily returns understate it whenever the candidate
    and the holdings close at different times (Burns, Engle & Mezrich 1998):
    a US share against Xetra-listed ETFs looked like a diversifier because
    each day's common move reaches New York hours after Frankfurt has closed
    (Discover run audit, 2026-09-28).
    """
    logger.debug("stage_portfolio_fit: %s", symbol)
    # Candidate returns
    cand_rows = market_history(db, symbol, days=_FIT_HISTORY_DAYS)
    if not cand_rows:
        return None, "portfolio_fit: no candidate price data"

    cand_df = pd.DataFrame(cand_rows).dropna(subset=["close"]).sort_values("date")
    # In EUR like the portfolio matrix: correlation as an investor in EUR sees it.
    from app.foundation.eur_prices import to_eur

    eur, _ccy = to_eur(db, symbol, {str(d)[:10]: float(c) for d, c in zip(cand_df["date"], cand_df["close"])})
    if eur:
        cand_df = pd.DataFrame({"date": list(eur), "close": list(eur.values())}).sort_values("date")
    if len(cand_df) < 60:
        return None, f"portfolio_fit: candidate only {len(cand_df)} rows"

    # Index the candidate by datetime64 up front so it aligns with the portfolio
    # matrix (build_real_price_matrix applies pd.to_datetime to its index). Without
    # this, intersecting datetime.date (object dtype) against a DatetimeIndex returns
    # an empty set on some pandas versions — which silently rejected every candidate.
    cand_df = cand_df.assign(date=pd.to_datetime(cand_df["date"]))
    cand_weekly = cand_df.set_index("date")["close"].astype(float).resample("W-FRI").last()
    cand_rets = cand_weekly.pct_change(fill_method=None).dropna()

    # Portfolio price matrix (build_real_price_matrix already yields a DatetimeIndex)
    port_df = build_real_price_matrix(db, user_id, lookback_days=_FIT_HISTORY_DAYS)
    if port_df is None or port_df.empty:
        # No portfolio → neutral fit
        return {"correlation": None, "overlap": None, "fit_score": 0.5}, None

    # Portfolio weighted return (equal weight fallback)
    port_rets = port_df.resample("W-FRI").last().pct_change(fill_method=None)
    port_weighted = pd.Series(port_rets.mean(axis=1)).dropna()

    common = cand_rets.index.intersection(port_weighted.index)  # type: ignore[union-attr]
    if len(common) < _FIT_MIN_WEEKS:
        # Fail open: too little overlap is a data-availability problem, not a
        # quality signal. Return a neutral fit so the candidate still reaches the
        # LLM rather than being silently dropped.
        return {
            "correlation": None,
            "overlap": None,
            "fit_score": 0.5,
            "note": f"only {len(common)} aligned weeks with portfolio",
        }, None

    corr = float(cand_rets.loc[common].corr(port_weighted.loc[common]))  # type: ignore[union-attr]
    if pd.isna(corr):
        corr = 0.0

    # ETF overlap check
    overlap = 0.0
    if is_etf:
        from app.foundation.etf_lookup import get_etf_composition
        comp = get_etf_composition(symbol)
        if comp and comp.holdings:
            cand_holdings = [{"ticker": hh.ticker, "weight": hh.weight} for hh in comp.holdings]
            # Held ETFs
            from app.foundation.models.entities import Holding, Portfolio
            portfolio = db.query(Portfolio).filter(Portfolio.user_id == user_id).first()
            if portfolio:
                held_etfs = (
                    db.query(Holding)
                    .filter(
                        Holding.portfolio_id == portfolio.id,
                        Holding.asset_type == "etf",
                    )
                    .all()
                )
                max_overlap = 0.0
                for h in held_etfs:
                    if not h.ticker:
                        continue
                    held_comp = get_etf_composition(h.ticker)
                    if held_comp and held_comp.holdings:
                        held_list = [{"ticker": hh.ticker, "weight": hh.weight} for hh in held_comp.holdings]
                        try:
                            ov = pairwise_overlap(cand_holdings, held_list)
                            max_overlap = max(max_overlap, ov.get("overlap", 0.0))
                        except Exception:
                            continue
                overlap = max_overlap

    # Fit score: 0-1, higher for low correlation and low overlap
    corr_penalty = max(0.0, min(1.0, (corr + 1.0) / 2.0))  # map [-1,1] -> [0,1]
    overlap_penalty = overlap
    fit_score = 1.0 - (0.6 * corr_penalty + 0.4 * overlap_penalty)
    fit_score = max(0.0, min(1.0, fit_score))

    scores = {
        "correlation": round(corr, 4),
        "correlation_basis": "weekly",
        "correlation_weeks": len(common),
        "overlap": round(overlap, 4) if is_etf else None,
        "fit_score": round(fit_score, 4),
    }
    logger.debug("stage_portfolio_fit: %s fit_score=%.2f corr=%.2f overlap=%s", symbol, fit_score, corr, overlap if is_etf else "n/a")
    return scores, None


# The universe's latest raw features and the pooled model, loaded once per
# day and shared by a run's candidates.
_ML_CACHE: dict[str, Any] = {}
_ML_LOCK = threading.Lock()
# A candidate's last bar may trail the universe's by this many days.
_ML_MAX_AGE_DAYS = 7


def _pooled_serving(db: Session) -> dict[str, Any]:
    """Today's pooled model row, fitted pipeline and universe features.

    ``{"row": None}`` when no pooled model was ever trained; ``model`` is
    None unless the newest row passed its gate (``status="completed"``).
    """
    from app.lab.quant_lab import pooled_ml as pooled
    from app.lab.quant_ml.registry import load_model

    today = datetime.now(UTC).date()
    with _ML_LOCK:
        if _ML_CACHE.get("day") == today:
            return _ML_CACHE
    row = pooled.latest_pooled_row(db)
    state: dict[str, Any] = {
        "day": today, "row": row, "model": None, "universe": None,
        "status": row.status if row is not None else "no_model",
    }
    if row is not None and row.status == "completed" and row.artefact_path:
        base = _exposure_base(db)
        if base is None:
            state["status"] = "no_universe_panel"
        else:
            state["model"] = load_model(row.id, base=str(Path(row.artefact_path).parent.parent))
            state["universe"] = pooled.latest_raw_features(base["panel"]["close"])
    with _ML_LOCK:
        _ML_CACHE.clear()
        _ML_CACHE.update(state)
        return _ML_CACHE


def stage_ml_signal(db: Session, symbol: str) -> tuple[dict[str, Any] | None, str | None]:
    """Advisory ML score from the pooled cross-sectional model (ADR 0015 #24).

    The model (``lab.quant_lab.pooled_ml``) ranks the Euro Stoxx 50 + S&P 100 on
    their 20-day forward volatility-scaled return; the weekly
    ``discover_ml_training`` job refits it and serves it only when its
    purged walk-forward rank IC clears the gate. Here the candidate's
    price features are ranked within today's universe cross-section and its
    ``prediction`` is the percentile (0-1) of its predicted rank.

    Until 2026-09-28 this read one triple-barrier classifier per ticker.
    All of them were rejected against the majority-class baseline: ~700
    rows of one stock's history carry no learnable direction.

    Never trains and never rejects. Without a validated model the stage
    returns ``prediction=None`` (composite drops the signal) and carries
    the macro regime context as a non-directional substitute, plus
    ``ml_status`` naming why (``no_model`` or the newest row's status).
    """
    logger.debug("stage_ml_signal: %s", symbol)
    gate_ctx: dict[str, Any] = MacroRegimeGate(db).context_dict() if db is not None else {}
    neutral: dict[str, Any] = {
        "prediction": None,
        "signal_kind": "regime_context",
        "regime_context": gate_ctx,
        "ml_status": "no_model",
        "concerns": ["ml_signal_unavailable"],
    }
    try:
        from app.lab.quant_lab import pooled_ml as pooled

        serving = _pooled_serving(db)
        row = serving["row"]
        if row is None:
            return neutral, None
        if serving["model"] is None or serving["universe"] is None or serving["universe"].empty:
            return {**neutral, "ml_status": str(serving["status"]), "model_id": row.id}, None

        universe: pd.DataFrame = serving["universe"]
        if symbol in universe.index:
            raw = universe
        else:
            own = alpha_build_panel(
                db, [symbol], datetime.now(UTC) - timedelta(days=_EXPOSURE_LOOKBACK_DAYS), datetime.now(UTC),
                include_fundamentals=False,
            ) if alpha_build_panel is not None else None
            if not own or "close" not in own or symbol not in own["close"].columns:
                return {**neutral, "ml_status": "no_bars", "model_id": row.id}, None
            mine = pooled.latest_raw_features(own["close"])
            if symbol not in mine.index:
                return {**neutral, "ml_status": "short_history", "model_id": row.id}, None
            raw = pd.concat([universe, mine])
        as_of = pd.to_datetime(raw["as_of"])
        fresh = cast(pd.DataFrame, raw.loc[as_of >= as_of.max() - pd.Timedelta(days=_ML_MAX_AGE_DAYS)])
        if symbol not in fresh.index or len(fresh) < pooled.MIN_CROSS_SECTION:
            return {**neutral, "ml_status": "stale_or_small_cross_section", "model_id": row.id}, None

        percentile = pooled.score_cross_section(serving["model"], fresh)
        return {
            "prediction": round(float(percentile[symbol]), 4),
            "model_id": row.id,
            "signal_kind": "ml_pooled",
            "cross_section": len(fresh),
            "ml_status": "completed",
            "regime_context": gate_ctx,
            "concerns": [],
        }, None
    except Exception as exc:
        logger.info("stage_ml_signal: failed for %s: %s", symbol, exc)
        return {**neutral, "ml_status": "error", "concerns": [f"ml_signal_error: {exc}"]}, None


# ---------------------------------------------------------------------------
# Stage 5c/5d — IBES estimate-revision / insider-trading signals.
# Advisory-only, ticker-matched (see pit_panel_joins module docstring for the
# "no verified crosswalk" caveat), never reject: a missing match means "no
# opinion", surfaced to composite.py as a dropped-not-neutral signal exactly
# like ml_signal above.
# ---------------------------------------------------------------------------


def stage_estimate_revision_signal(
    db: Session,
    symbol: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """SUE / estimate-revision momentum / dispersion for *symbol*.

    The WRDS IBES extract first, when it has a current consensus change
    (it is exported by hand; past its 92-day staleness limit it has none).
    Otherwise the 30-day change in Yahoo's current-year consensus EPS
    (``data_backbone.analyst_estimates``, owner decision 2026-09-29): the
    same quantity as IBES's monthly revision, without SUE. The source is
    recorded as ``estimate_source``.
    """
    logger.debug("stage_estimate_revision_signal: %s", symbol)
    neutral: dict[str, Any] = {
        "sue": None,
        "revision_momentum": None,
        "dispersion": None,
        "data_confidence": None,
        "estimate_source": None,
        "concerns": ["estimate_data_unavailable"],
    }
    dispersion: float | None = None
    try:
        joined = pit_ibes_estimate_signal_for_symbol(db, symbol, pd.DatetimeIndex([pd.Timestamp.now(UTC)]))
        if joined is not None and not joined.empty:
            row = joined.iloc[0]
            values = {
                "sue": _safe_float(row.get("sue")),
                "revision_momentum": _safe_float(row.get("revision_momentum")),
                "dispersion": _safe_float(row.get("dispersion")),
            }
            dispersion = values["dispersion"]
            if values["sue"] is not None or values["revision_momentum"] is not None:
                return {
                    **values,
                    "data_confidence": row.get("data_confidence"),
                    "estimate_source": "ibes",
                    "concerns": [],
                }, None
    except Exception as exc:
        logger.info("stage_estimate_revision_signal: IBES failed for %s: %s", symbol, exc)
    try:
        from app.foundation.data_backbone.analyst_estimates import live_revision

        live = live_revision(db, symbol)
    except Exception as exc:
        logger.info("stage_estimate_revision_signal: live estimates failed for %s: %s", symbol, exc)
        live = None
    if live is None:
        # Matched in IBES without a current change (stale, or the first
        # month after a fiscal-year roll), and no usable live consensus.
        return {**neutral, "dispersion": dispersion}, None
    return {
        "sue": None,
        "revision_momentum": live["revision_momentum"],
        "dispersion": dispersion,
        "data_confidence": "symbol_match",
        "estimate_source": live["source"],
        "analysts": live["analysts"],
        "concerns": [],
    }, None


def stage_insider_signal(
    db: Session,
    symbol: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """SEC-derived opportunistic insider cluster-buy / net-flow signal for *symbol*."""
    logger.debug("stage_insider_signal: %s", symbol)
    neutral: dict[str, Any] = {
        "cluster_buy_score": None,
        "net_insider_flow_usd": None,
        "data_confidence": None,
        "concerns": ["insider_data_unavailable"],
    }
    try:
        joined = pit_insider_signal_for_symbol(db, symbol, pd.DatetimeIndex([pd.Timestamp.now(UTC)]))
        if joined is None or joined.empty:
            return neutral, None
        row = joined.iloc[0]
        cluster = _safe_float(row.get("cluster_buy_score"))
        if cluster is None:
            # Matched, but today lies outside what the extract covers.
            return neutral, None
        return {
            "cluster_buy_score": cluster,
            "net_insider_flow_usd": _safe_float(row.get("net_insider_flow_usd")),
            "data_confidence": row.get("data_confidence"),
            "concerns": [],
        }, None
    except Exception as exc:
        logger.info("stage_insider_signal: failed for %s: %s", symbol, exc)
        return {**neutral, "concerns": [f"insider_signal_error: {exc}"]}, None


# ---------------------------------------------------------------------------
# Streaming candidate pipeline (generator)
# ---------------------------------------------------------------------------


def run_candidate_pipeline(
    db: Session,
    user_id: str,
    candidates: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    *,
    cancel_token: Any | None = None,
    results_out: list[dict[str, Any]] | None = None,
    preingest_progress: Any | None = None,
    run_id: str | None = None,
) -> Generator[dict[str, Any], None, list[dict[str, Any]]]:
    """Generator that processes each candidate and yields per-candidate progress.

    Each yield has keys: ``index``, ``total``, ``symbol``, ``stage``,
    ``shortlisted_so_far``, ``benchmark_ingest`` (warm-up summary for the
    candidate + benchmark ETF price-cache pre-ingest).

    After exhausting, returns the top ``LLM_MAX_CANDIDATES`` candidates ranked by
    composite score (quality gates are advisory; only no-data candidates drop out).

    If ``results_out`` is provided, full per-candidate result dicts are
    appended to it for the caller to use (avoids re-mapping).

    ``run_id`` is passed on to ``stage_alpha_miner``, which since 2026-09-28
    scores validated factors and writes no ac_trial_ledger rows.
    """
    logger.info("run_candidate_pipeline: %d candidates for user %s", len(candidates), user_id)
    total = len(candidates)
    shortlisted: list[dict[str, Any]] = []
    instrument_types: dict[str, str] = {}

    # Macro regime gate: pause discovery in bear/crisis regimes.
    regime_gate = MacroRegimeGate(db)
    if regime_gate.blocks_discovery():
        logger.warning(
            "run_candidate_pipeline: macro regime gate blocked discovery for user %s (regime=%s)",
            user_id,
            regime_gate.context_dict().get("regime_label"),
        )
        return []

    # Pre-ingest bar-price history for all candidates in parallel so that
    # the per-candidate stage loop hits warm DB caches.  Each worker opens
    # its own session; the caller's `db` session is never shared.
    # Note: _preingest_candidates also warms the benchmark ETFs (see its
    # docstring) — they are intentionally absent from `symbols` below.
    symbols = [c["symbol"] for c in candidates]
    benchmark_ingest = _preingest_candidates(symbols, progress=preingest_progress)

    # Wire each candidate's provider-sourced ISIN (DiscoverCandidate.isin,
    # already in scope per-candidate below) into the security master so the
    # PIT gvkey joins (stage_quant_signals, stage_ml_signal) can resolve a
    # gvkey from `symbol` alone via resolve_isin_for_symbol -- batched so a
    # ~300-candidate run costs a handful of OpenFIGI HTTP calls, not one per
    # candidate. Best-effort: never raises, writes what it can.
    resolve_securities_batch(
        db,
        [(c["symbol"], c.get("isin")) for c in candidates],
        alias_source="discover_pipeline",
    )

    for idx, cand in enumerate(candidates):
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()

        symbol = cand["symbol"]
        isin = cand.get("isin")
        source = cand.get("source", "unknown")
        name = cand.get("name", symbol)
        instrument_type = classify_instrument(symbol, source, name)
        is_etf = instrument_type in ("etf", "money_market", "bond")

        scores: dict[str, Any] = {}
        reject_stage: str | None = None
        reject_reason: str | None = None

        # Stage 1
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_history_ingest(db, symbol)
            if r:
                reject_stage = "history_ingest"
                reject_reason = r
            else:
                scores["history_ingest"] = s

        # Stage 1a — AlphaCrafter miner
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_alpha_miner(db, symbol, run_id=run_id)
            if r:
                reject_stage = "alpha_miner"
                reject_reason = r
            else:
                scores["alpha_miner"] = s

        # Stage 1b — AlphaCrafter screener
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_alpha_screener(db, symbol)
            if r:
                reject_stage = "alpha_screener"
                reject_reason = r
            else:
                scores["alpha_screener"] = s

        # Stage 1c — sentiment + fundamentals
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_sentiment_fundamentals(db, symbol, is_etf=is_etf)
            if r:
                reject_stage = "sentiment_fundamentals"
                reject_reason = r
            else:
                scores["sentiment_fundamentals"] = s

        # Stage 2 (backward-looking)
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_momentum_quality(db, symbol)
            if r:
                reject_stage = "momentum_quality"
                reject_reason = r
            else:
                scores["momentum_quality"] = s

        # Stage 3
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_backtest_vs_benchmark(db, symbol, is_etf=is_etf)
            if r:
                reject_stage = "backtest_vs_benchmark"
                reject_reason = r
            else:
                scores["backtest_vs_benchmark"] = s

        # Stage 4
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_verification_gate(db, symbol)
            if r:
                reject_stage = "verification_gate"
                reject_reason = r
            else:
                scores["verification_gate"] = s

        # Stage 4b — deterministic quant signals (load-bearing)
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_quant_signals(db, symbol, is_etf=is_etf)
            if r:
                reject_stage = "quant_signals"
                reject_reason = r
            else:
                if s is not None and instrument_type in ("money_market", "bond"):
                    # stage_quant_signals only distinguishes etf/equity;
                    # restore the finer classification so composite.py can
                    # exclude equity-shaped signals for cash equivalents and
                    # bond funds keep their duration-bearing treatment
                    # (expected_return anchors, shrinkage) downstream.
                    s["instrument_type"] = instrument_type
                scores["quant_signals"] = s

        # Stage 5
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_portfolio_fit(db, user_id, symbol, is_etf=is_etf)
            if r:
                reject_stage = "portfolio_fit"
                reject_reason = r
            else:
                scores["portfolio_fit"] = s

        # Stage 5b — ML signal (Track D1c), advisory-only, never rejects
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_ml_signal(db, symbol)
            if r is None:
                scores["ml_signal"] = s

        # Stage 5c/5d — IBES estimate-revision / insider-trading signals,
        # advisory-only, ticker-matched, never reject.
        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_estimate_revision_signal(db, symbol)
            if r is None:
                scores["estimate_revision_signal"] = s

        if reject_stage is None:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            s, r = stage_insider_signal(db, symbol)
            if r is None:
                scores["insider_signal"] = s

        # Composite score + aggregated concerns. Quality gates are advisory; the
        # composite is what ranks candidates for the LLM. The deterministic
        # quant signals (momentum, risk, benchmark-relative, portfolio fit) are
        # load-bearing; the AlphaCrafter ic_icir signal is advisory and drops
        # out of the weighting entirely when unavailable/too thin.
        composite = 0.0
        concerns: list[str] = []
        # Aggregation runs for REJECTED candidates too: their stage concerns
        # (e.g. verification_gate drawdown flags) must reach scores_json so the
        # UI Flags column explains WHY they were rejected. Only the composite
        # computation is shortlist-only.
        for stage_key in (
            "history_ingest", "alpha_miner", "alpha_screener",
            "sentiment_fundamentals", "momentum_quality", "backtest_vs_benchmark",
            "verification_gate", "quant_signals", "portfolio_fit", "ml_signal",
            "estimate_revision_signal", "insider_signal",
        ):
            stage_val = scores.get(stage_key)
            if isinstance(stage_val, dict):
                stage_concerns = stage_val.get("concerns")
                if isinstance(stage_concerns, list):
                    concerns.extend(stage_concerns)

        # The composite itself is computed after the loop: the momentum
        # signal is a rank against every other evaluable candidate, so it
        # cannot be known until all of them have been scored.

        result = {
            "symbol": symbol,
            "isin": isin,
            "name": name,
            "source": source,
            "scores": scores,
            "concerns": concerns,
            "reject_stage": reject_stage,
            "reject_reason": reject_reason,
            "composite_score": round(composite, 4),
        }

        if results_out is not None:
            results_out.append(result)

        if reject_stage is None:
            shortlisted.append(result)
            instrument_types[symbol] = instrument_type

        yield {
            "index": idx + 1,
            "total": total,
            "symbol": symbol,
            "stage": reject_stage or "completed",
            "shortlisted_so_far": len(shortlisted),
            "benchmark_ingest": benchmark_ingest,
        }

    if cancel_token is not None:
        cancel_token.raise_if_cancelled()

    # Cross-sectional pass: momentum and analyst ranks, then composites,
    # then the recency penalty (which needs the pre-penalty composite).
    assign_momentum_ranks(
        [r["scores"] for r in shortlisted if instrument_types.get(r["symbol"]) != "money_market"]
    )
    assign_analyst_ranks([r["scores"] for r in shortlisted])
    signal_config = get_or_seed_active_config(db, "signal_weights")
    cfg_json = signal_config.config_json if isinstance(signal_config.config_json, dict) else {}
    signal_weights = cfg_json.get("weights") if isinstance(cfg_json, dict) else None
    for result in shortlisted:
        scores = result["scores"]
        alpha_miner = scores.get("alpha_miner")
        if not isinstance(alpha_miner, dict):
            alpha_miner = {}
        signals = derive_signals_from_scores(scores)
        composite = compute_weighted_composite(signals, ic_data=alpha_miner, weights=signal_weights)
        scores["composite_raw"] = round(composite, 4)
        scores["composite_inputs"] = composite_contributions(
            signals, ic_data=alpha_miner, weights=signal_weights,
        )
        composite, recency_detail = _apply_recency_penalty(
            db, user_id, result["symbol"], instrument_types.get(result["symbol"], "equity"), composite,
        )
        if recency_detail:
            scores["recency_penalty"] = recency_detail
        result["composite_score"] = round(composite, 4)

    # Rank all evaluable candidates by composite and hand the best N to the LLM,
    # at most sector_cap_for(N) per sector. Quality gates are advisory now, so
    # "evaluable" means the candidate had enough data to score — not that it
    # cleared a quality bar.
    shortlisted.sort(key=lambda x: x["composite_score"], reverse=True)
    selected, capped = apply_sector_cap(shortlisted, LLM_MAX_CANDIDATES)
    for result in capped:
        result["sector_capped"] = True
    logger.info(
        "run_candidate_pipeline: %d evaluable of %d, handing top %d to LLM (%d skipped by sector cap)",
        len(shortlisted), total, len(selected), len(capped),
    )
    return selected


# ---------------------------------------------------------------------------
# Cross-sectional helpers (momentum rank, sector cap)
# ---------------------------------------------------------------------------

# Below this many rankable candidates a percentile rank is too coarse to mean
# much (ad-hoc single-symbol runs, tests); composite.py then falls back to
# its tanh squash of the raw 12-1m return.
MOMENTUM_RANK_MIN_CANDIDATES = 20

# At most this fraction of the shortlist may come from one sector (floor 2):
# 3 of 15. The 2026-09-25 shortlist held 9 European banks and 5
# energy/utility names. Individual-stock momentum is largely industry
# momentum (Moskowitz & Grinblatt 1999), so an uncapped momentum-weighted
# ranking turns into one sector bet.
SECTOR_CAP_FRACTION = 0.2


def assign_momentum_ranks(score_dicts: list[dict[str, Any]]) -> None:
    """Stamp ``momentum_quality.momentum_rank`` (mid-rank percentile in (0, 1)).

    Ranks are written into the stored scores, not only used in memory, so
    ``derive_signals_from_scores`` replays (config_review) reproduce the live
    momentum signal. No-op below ``MOMENTUM_RANK_MIN_CANDIDATES``.
    """
    rankable = [
        (sd["momentum_quality"], float(sd["momentum_quality"]["momentum_12_1m"]))
        for sd in score_dicts
        if isinstance(sd.get("momentum_quality"), dict)
        and isinstance(sd["momentum_quality"].get("momentum_12_1m"), (int, float))
    ]
    n = len(rankable)
    if n < MOMENTUM_RANK_MIN_CANDIDATES:
        return
    ranks = pd.Series([m for _, m in rankable]).rank(method="average")
    for (mom, _), rank in zip(rankable, ranks):
        mom["momentum_rank"] = round((float(rank) - 0.5) / n, 4)


# A sector needs at least this many candidates with a price target before
# analyst upside is ranked within it; smaller groups rank against the pool.
ANALYST_RANK_MIN_SECTOR = 5


def assign_analyst_ranks(score_dicts: list[dict[str, Any]]) -> None:
    """Stamp ``sentiment_fundamentals.analyst_rank``: the percentile of target
    upside within the candidate's sector.

    Raw upside is a poor absolute signal: consensus targets sit well above
    prices on average (Bradshaw, Brown & Huang 2013), so ``0.5 + 2*upside``
    pinned most names with coverage near 1.0 once the data actually arrived.
    Target prices carry information mainly as relative valuations within an
    industry (Da & Schaumburg 2011), so the rank is taken within the sector.
    Sectors with fewer than ``ANALYST_RANK_MIN_SECTOR`` targets are ranked
    against the whole pool. No-op below ``MOMENTUM_RANK_MIN_CANDIDATES`` names
    with an upside; composite.py then keeps the absolute score.
    """
    rankable = [
        (sd["sentiment_fundamentals"], float(sd["sentiment_fundamentals"]["analyst_upside"]))
        for sd in score_dicts
        if isinstance(sd.get("sentiment_fundamentals"), dict)
        and isinstance(sd["sentiment_fundamentals"].get("analyst_upside"), (int, float))
    ]
    if len(rankable) < MOMENTUM_RANK_MIN_CANDIDATES:
        return

    def _stamp(group: list[tuple[dict[str, Any], float]]) -> None:
        ranks = pd.Series([u for _, u in group]).rank(method="average")
        for (sf, _), rank in zip(group, ranks):
            sf["analyst_rank"] = round((float(rank) - 0.5) / len(group), 4)

    by_sector: dict[str, list[tuple[dict[str, Any], float]]] = {}
    for sf, upside in rankable:
        sector = sf.get("sector")
        if isinstance(sector, str) and sector:
            by_sector.setdefault(sector, []).append((sf, upside))
    _stamp(rankable)  # pool-wide first; overwritten below where a sector is big enough
    for group in by_sector.values():
        if len(group) >= ANALYST_RANK_MIN_SECTOR:
            _stamp(group)


def candidate_sector(result: dict[str, Any]) -> str | None:
    """Sector recorded by stage_sentiment_fundamentals, or None (funds, gaps)."""
    scores = result.get("scores")
    sf = scores.get("sentiment_fundamentals") if isinstance(scores, dict) else None
    sector = sf.get("sector") if isinstance(sf, dict) else None
    return sector if isinstance(sector, str) and sector else None


def sector_cap_for(limit: int) -> int:
    return max(2, int(limit * SECTOR_CAP_FRACTION))


def apply_sector_cap(
    ranked: list[dict[str, Any]],
    limit: int,
    already_selected: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Take up to *limit* results from *ranked* (best first), at most
    ``sector_cap_for(limit)`` per sector counting *already_selected*.

    Returns ``(selected, capped)``: *capped* holds the better-ranked results
    skipped because their sector was full, so callers can say why. Results
    without a sector (funds, missing fundamentals) are never capped.
    """
    cap = sector_cap_for(limit)
    counts: dict[str, int] = {}
    for result in already_selected or []:
        sector = candidate_sector(result)
        if sector is not None:
            counts[sector] = counts.get(sector, 0) + 1
    selected: list[dict[str, Any]] = []
    capped: list[dict[str, Any]] = []
    for result in ranked:
        if len(selected) + len(already_selected or []) >= limit:
            break
        sector = candidate_sector(result)
        if sector is not None and counts.get(sector, 0) >= cap:
            capped.append(result)
            continue
        if sector is not None:
            counts[sector] = counts.get(sector, 0) + 1
        selected.append(result)
    return selected, capped
