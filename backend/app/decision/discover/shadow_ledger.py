"""Shadow ledger capture and the Discover provenance stamp (ADR 0018 §5, §10).

Every universe member of a run is frozen in ``discover_candidate_snapshot``,
not only the ~15 picks: a stock's forward return is observable whether or not
it was picked, so recording the whole cross-section turns one noisy basket per
week into ~200 observations of whether the composite sorts winners from
losers (IR ≈ IC·√breadth). The rows are built from the pipeline's in-memory
results, never from ``DiscoverCandidate`` (later stages rewrite those).

The stamp's cohort holds everything that changes what Discover's calls test:
the resolved signal-weight config, the composite and universe rule versions,
the calibrator rule and the horizon. The LLM is deliberately not in it: it
only writes the dossier and never touches the composite, the pick or the
ranges. Its identity is still recorded, as a detail.

**Bump** :data:`COMPOSITE_FORMULA_VERSION` whenever ``composite.py`` or the
pipeline's ranking changes meaning, and :data:`UNIVERSE_RULE_VERSION` whenever
``universe.py`` changes which names are scored. Each bump starts a new cohort,
and every cohort is one trial.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.decision.discover.calibrator import CALIBRATOR_VERSION
from app.foundation import provenance
from app.foundation.instrument_taxonomy import classify_instrument
from app.foundation.market import history
from app.foundation.models.entities import (
    DiscoverCandidate,
    DiscoverCandidateSnapshot,
    DiscoverRun,
    DiscoveryPrediction,
)
from app.foundation.llm.sampling import QWEN_SAMPLING

logger = logging.getLogger(__name__)

#: ADR 0017's composite (de-duplicated momentum, analyst ranks within sector,
#: recency penalty without the strong-conviction exemption).
COMPOSITE_FORMULA_VERSION = "adr0017.1"
UNIVERSE_RULE_VERSION = 1

_ETF_TYPES = frozenset({"etf", "money_market", "bond"})


def instrument_group(symbol: str, source: str | None, name: str | None) -> str:
    kind = classify_instrument(symbol, source or "unknown", name or symbol)
    if kind == "equity":
        return "stock"
    return "etf" if kind in _ETF_TYPES else "other"


def discover_stamp(
    db: Session,
    *,
    run_id: str,
    signal_config: dict[str, Any] | None,
    signal_config_id: str | None,
    prompt_config: dict[str, Any] | None,
    horizon_days: int,
    degradation_flags: list[str] | None = None,
) -> dict[str, Any]:
    """The provenance stamp shared by a run's predictions and snapshots."""
    llm = provenance.llm_server_identity(db)
    flags = list(degradation_flags or [])
    if not llm.get("available"):
        flags.append("llm_identity_unavailable")
    return provenance.stamp(
        source="discover",
        cohort_spec={
            "composite_formula_version": COMPOSITE_FORMULA_VERSION,
            "universe_rule_version": UNIVERSE_RULE_VERSION,
            "signal_config_hash": provenance.canonical_hash(signal_config or {}),
            "calibrator_version": CALIBRATOR_VERSION,
            "horizon_days": int(horizon_days),
        },
        details={
            "run_id": run_id,
            "config_id": signal_config_id,
            # Dossier text only: recorded, not part of the cohort.
            "dossier_llm": {
                **llm,
                "prompt_template_hash": provenance.canonical_hash(prompt_config or {}),
                "client_sampling": dict(QWEN_SAMPLING),
            },
        },
        degradation_flags=flags,
    )


def _last_close(db: Session, symbol: str) -> tuple[float | None, date | None]:
    """Last cached close and its date (no live fetch: the pipeline just warmed the cache)."""
    try:
        rows = history(db, symbol, days=14, allow_live=False)
    except Exception as exc:  # noqa: BLE001 - a missing price is recorded as missing
        logger.debug("shadow_ledger: no cached close for %s: %s", symbol, exc)
        return None, None
    for row in reversed(rows or []):
        close, day = row.get("close"), row.get("date")
        if close is None or day is None or float(close) <= 0:
            continue
        if isinstance(day, datetime):
            day = day.date()
        elif isinstance(day, str):
            day = date.fromisoformat(day[:10])
        return float(close), day
    return None, None


def _sector(result: dict[str, Any]) -> str | None:
    scores = result.get("scores")
    sf = scores.get("sentiment_fundamentals") if isinstance(scores, dict) else None
    sector = sf.get("sector") if isinstance(sf, dict) else None
    return sector[:64] if isinstance(sector, str) and sector else None


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def capture_snapshot(
    db: Session,
    *,
    run_id: str,
    user_id: str,
    issued_at: datetime,
    pipeline_results: list[dict[str, Any]],
    shortlisted_symbols: set[str],
    picked_symbols: set[str],
    stamp: dict[str, Any],
    backfilled: bool = False,
) -> int:
    """Write one frozen snapshot row per pipeline result; returns the number written.

    Idempotent per run: a run that already has snapshot rows is left alone
    (the rows are append-only). The caller owns the commit.
    """
    if db.query(DiscoverCandidateSnapshot.id).filter(DiscoverCandidateSnapshot.run_id == run_id).first():
        return 0
    issued = issued_at if issued_at.tzinfo else issued_at.replace(tzinfo=UTC)
    issue_day = issued.astimezone(UTC).date()

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in pipeline_results:
        symbol = result.get("symbol")
        if not isinstance(symbol, str) or not symbol or symbol in seen:
            continue
        seen.add(symbol)
        evaluable = result.get("reject_stage") is None
        raw_scores = result.get("scores")
        scores: dict[str, Any] = raw_scores if isinstance(raw_scores, dict) else {}
        rows.append({
            "result": result,
            "symbol": symbol,
            "group": instrument_group(symbol, result.get("source"), result.get("name")),
            "evaluable": evaluable,
            "composite": _float(result.get("composite_score")) if evaluable else None,
            "composite_raw": _float(scores.get("composite_raw")) if evaluable else None,
            "scores": scores,
        })

    ranked = sorted(
        (r for r in rows if r["evaluable"] and r["group"] == "stock" and r["composite"] is not None),
        key=lambda r: (-r["composite"], r["symbol"]),
    )
    for i, r in enumerate(ranked, start=1):
        r["stock_rank"] = i
    n_stocks = len(ranked)

    for r in rows:
        result = r["result"]
        # A back-filled run's entry is resolved from history later, not from today's cache.
        entry_close, entry_day = (None, None) if backfilled else _last_close(db, r["symbol"])
        components: dict[str, Any] = {}
        if r["evaluable"]:
            components["composite_inputs"] = r["scores"].get("composite_inputs")
            if r["scores"].get("recency_penalty"):
                components["recency_penalty"] = r["scores"]["recency_penalty"]
        concerns = result.get("concerns")
        if isinstance(concerns, list) and concerns:
            components["concerns"] = [str(c)[:200] for c in concerns[:20]]
        db.add(DiscoverCandidateSnapshot(
            user_id=user_id,
            run_id=run_id,
            issued_at=issued,
            issue_date=issue_day,
            symbol=r["symbol"][:32],
            isin=result.get("isin"),
            source=(result.get("source") or None),
            instrument_group=r["group"],
            sector=_sector(result),
            evaluable=r["evaluable"],
            reject_stage=result.get("reject_stage"),
            reject_reason=result.get("reject_reason"),
            composite=r["composite"],
            composite_raw=r["composite_raw"],
            components_json=components,
            stock_rank=r.get("stock_rank"),
            n_evaluable_stocks=n_stocks,
            shortlisted=r["symbol"] in shortlisted_symbols,
            sector_capped=bool(result.get("sector_capped")),
            picked=r["symbol"] in picked_symbols,
            entry_close=entry_close,
            entry_close_date=entry_day,
            cohort_id=stamp.get("cohort_id") or "stamp_failed",
            provenance_json=stamp,
            backfilled=backfilled,
        ))
    db.flush()
    logger.info(
        "shadow_ledger: froze %d candidates for run %s (%d evaluable stocks)", len(rows), run_id, n_stocks
    )
    return len(rows)


#: Rejections made after the pipeline scored a name: still evaluable.
_POST_PIPELINE_STAGES = frozenset({"sector_cap", "shortlist_limit", "tradeability_gate"})


def _scores(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def backfill_snapshots(db: Session, *, user_id: str | None = None) -> dict[str, int]:
    """Rebuild snapshots of past runs from their ``DiscoverCandidate`` rows (ADR 0018 §5).

    Exploratory only: every row is ``backfilled = true`` with cohort
    ``legacy_unstamped`` and never enters an e-value. Caveats that the page
    states: the composite formula changed between runs, ``scores_json`` was
    rewritten for shortlisted names, the shortlist is the run's final one, and
    evaluability is reconstructed from ``reject_stage`` (rejections after
    scoring count as evaluable). Runs that already have snapshots are skipped,
    so this is idempotent. The caller owns the commit.
    """
    runs_query = db.query(DiscoverRun).filter(DiscoverRun.status == "completed")
    if user_id is not None:
        runs_query = runs_query.filter(DiscoverRun.user_id == user_id)
    done = {r for (r,) in db.query(DiscoverCandidateSnapshot.run_id).distinct()}
    written_runs = written_rows = 0
    for run in runs_query.all():
        if run.id in done or run.created_at is None:
            continue
        candidates = db.query(DiscoverCandidate).filter(DiscoverCandidate.run_id == run.id).all()
        if not candidates:
            continue
        picked = {
            s for (s,) in db.query(DiscoveryPrediction.symbol).filter(
                DiscoveryPrediction.run_id == run.id, DiscoveryPrediction.portfolio_id.is_(None),
            )
        }
        results = []
        for c in candidates:
            scores = _scores(c.scores_json)
            evaluable = c.reject_stage is None or c.reject_stage in _POST_PIPELINE_STAGES
            results.append({
                "symbol": c.symbol, "isin": c.isin, "name": c.name, "source": c.source, "scores": scores,
                "concerns": scores.get("concerns") or [],
                "reject_stage": None if evaluable else c.reject_stage,
                "reject_reason": None if evaluable else c.reject_reason,
                "composite_score": scores.get("composite"),
                "sector_capped": c.reject_stage == "sector_cap",
            })
        stamp = {
            "provenance_schema_version": provenance.PROVENANCE_SCHEMA_VERSION,
            "source": "discover",
            "cohort_id": provenance.LEGACY_COHORT,
            "backfilled": True,
            "degradation_flags": ["backfilled"],
        }
        written_rows += capture_snapshot(
            db, run_id=run.id, user_id=run.user_id, issued_at=run.created_at, pipeline_results=results,
            shortlisted_symbols={c.symbol for c in candidates if c.status == "shortlisted"},
            picked_symbols=picked, stamp=stamp, backfilled=True,
        )
        written_runs += 1
    logger.info("shadow_ledger: back-filled %d runs (%d rows)", written_runs, written_rows)
    return {"runs": written_runs, "rows": written_rows}
