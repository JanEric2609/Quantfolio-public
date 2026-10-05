"""AlphaCrafter Miner: factor candidate generation and validation (Phase 4).

Computes real cross-sectional **information coefficient (IC)**, **ICIR**,
**turnover** and **decay half-life** for each factor candidate over a configurable
index basket, then persists survivors to ``FactorsLibrary`` so the rest of the
pipeline (Screener -> Trader -> Dossier) can hand off by real database id.

Factor values come from two paths, both producing a (date x symbol) frame:

* **Seed factors** — name-addressable via :func:`quant_factors.compute_factor`.
* **DSL factors** — free-form formulas evaluated through the sandboxed
  :mod:`app.lab.alphacrafter.factor_dsl` (used for LLM proposals).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.foundation.models.entities import FactorsLibrary
from app.foundation import quant_factors
from app.lab.alphacrafter import factor_dsl
from app.lab.alphacrafter.panel import build_panel
from app.lab.alphacrafter.shared_memory import (
    FactorMetrics,
    FactorState,
    SharedMemoryH,
)
from app.foundation.data_backbone.ingest import DataIngester
from app.foundation.settings import get_public_settings

logger = logging.getLogger(__name__)

# Defaults; overridable via public settings (alphacrafter_*).
DEFAULT_LOOKBACK_YEARS = 5.0
DEFAULT_HORIZON_DAYS = 5
DEFAULT_MIN_IC = 0.02
DEFAULT_MIN_ICIR = 0.3


class MinerAgent:
    """Agent that evaluates factor candidates and writes results to SharedMemoryH."""

    def run(self, h: SharedMemoryH, db: Session, propose_llm: bool = False) -> SharedMemoryH:
        """Run the Miner agent: evaluate factors and persist to DB.

        Reads universe and date_range from h.market_state.
        Writes factor_states, miner_outputs, and agent_history to H.

        When *propose_llm* is true, extra DSL factors are fetched from the LLM
        proposer (best-effort, fail-open) and evaluated alongside the seed
        factors — mirroring ``run_miner``'s async-path behaviour so the
        SharedMemoryH pipeline honours ``alphacrafter_propose_llm`` too.
        """
        settings = get_public_settings(db)
        horizon = int(settings.get("alphacrafter_ic_horizon_days", DEFAULT_HORIZON_DAYS))
        min_ic = float(settings.get("alphacrafter_min_ic", DEFAULT_MIN_IC))
        min_icir = float(settings.get("alphacrafter_min_icir", DEFAULT_MIN_ICIR))

        universe = h.market_state.universe
        start_str, end_str = h.market_state.date_range
        start = datetime.fromisoformat(start_str)
        end = datetime.fromisoformat(end_str)

        panel = build_panel(db, universe, start, end, registry=None)

        if not panel:
            logger.warning("Miner: empty panel for universe %s", universe)
            h.miner_outputs = {"n_evaluated": 0, "n_valid": 0, "n_persisted": 0}
            h.append_history("miner", "run", {"n_evaluated": 0, "n_valid": 0, "n_persisted": 0})
            return h

        factor_defs = [
            {
                "name": d.get("name"),
                "formula": d.get("formula", ""),
                "source": d.get("source", "quant_factors"),
                "dsl": d.get("dsl"),
            }
            for d in quant_factors.get_factor_definitions()
        ]

        n_evaluated = 0
        n_valid = 0
        n_persisted = 0
        deterministic_count = 0
        llm_count = 0
        llm_status = "skipped"
        factor_states = []

        # 1. Evaluate and persist deterministic seed factors immediately
        for d in factor_defs:
            candidate = evaluate_candidate(
                panel,
                name=d["name"],
                formula=d["formula"],
                source=d["source"],
                dsl=d.get("dsl"),
                horizon=horizon,
                min_ic=min_ic,
                min_icir=min_icir,
            )
            n_evaluated += 1

            if candidate.valid:
                n_valid += 1
                persist_candidate(db, candidate)
                n_persisted += 1
                deterministic_count += 1

            fs = FactorState(
                id=candidate.id or f"candidate_{candidate.name}",
                name=candidate.name,
                source=candidate.source,
                category="unknown",
                formula=candidate.formula,
                dsl=candidate.dsl,
                metrics=FactorMetrics(
                    ic=candidate.ic,
                    icir=candidate.icir,
                    turnover=candidate.turnover,
                    decay_halflife_days=candidate.decay_halflife_days,
                    n_obs=len(candidate.ic_series),
                ),
                regime_applicability={},
            )
            factor_states.append(fs)

        # 2. LLM factor proposals (best-effort, fail-open, non-blocking)
        if propose_llm:
            try:
                # Dispatch safely across thread boundaries to the main event loop's PriorityQueue
                from app.foundation.llm import router as llm_router
                main_task = llm_router._worker_task
                main_loop = main_task.get_loop() if main_task and not main_task.done() else None

                if main_loop and main_loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(
                        _llm_extra_factors(db, panel, h.market_state.regime_label, settings),
                        main_loop,
                    )
                    extras = fut.result(timeout=45.0)
                else:
                    extras = asyncio.run(
                        asyncio.wait_for(
                            _llm_extra_factors(db, panel, h.market_state.regime_label, settings),
                            timeout=45.0,
                        )
                    )
                llm_status = "ok"
            except Exception as exc:
                logger.warning("Miner: LLM proposal fetch failed (non-fatal): %s", exc)
                extras = []
                llm_status = "failed"

            for extra in extras:
                candidate = evaluate_candidate(
                    panel,
                    name=extra.get("name", "unknown_extra"),
                    formula=extra.get("formula", ""),
                    source=extra.get("source", "llm"),
                    dsl=extra.get("dsl"),
                    horizon=horizon,
                    min_ic=min_ic,
                    min_icir=min_icir,
                )
                n_evaluated += 1

                if candidate.valid:
                    n_valid += 1
                    persist_candidate(db, candidate)
                    n_persisted += 1
                    llm_count += 1

                fs = FactorState(
                    id=candidate.id or f"candidate_{candidate.name}",
                    name=candidate.name,
                    source=candidate.source,
                    category="unknown",
                    formula=candidate.formula,
                    dsl=candidate.dsl,
                    metrics=FactorMetrics(
                        ic=candidate.ic,
                        icir=candidate.icir,
                        turnover=candidate.turnover,
                        decay_halflife_days=candidate.decay_halflife_days,
                        n_obs=len(candidate.ic_series),
                    ),
                    regime_applicability={},
                )
                factor_states.append(fs)

        h.factor_states = factor_states
        h.miner_outputs = {
            "n_evaluated": n_evaluated,
            "n_valid": n_valid,
            "n_persisted": n_persisted,
            "deterministic_count": deterministic_count,
            "llm_count": llm_count,
            "llm_status": llm_status,
        }
        h.append_history(
            "miner",
            "run",
            h.miner_outputs,
        )

        return h


@dataclass
class FactorCandidate:
    """A proposed factor with real validation metrics."""

    name: str
    formula: str
    source: str
    ic: float
    icir: float
    turnover: float
    decay_halflife_days: float | None = None
    valid: bool = True
    validation_notes: str = ""
    dsl: str | None = None
    ic_series: list[float] = field(default_factory=list)
    # Database id once persisted to FactorsLibrary (None until then).
    id: str | None = None


# --- Pure metric helpers (synchronous, unit-testable) -------------------------


def forward_returns(close: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Forward simple return over ``horizon`` days, aligned to the entry date."""
    return close.shift(-horizon) / close - 1.0


def cross_sectional_ic(factor_values: pd.DataFrame, fwd_returns: pd.DataFrame) -> pd.Series:
    """Per-date cross-sectional **Spearman** IC between factor and forward returns.

    Spearman is implemented as the Pearson correlation of cross-sectional ranks,
    which avoids a hard SciPy dependency.
    """
    common = factor_values.index.intersection(fwd_returns.index)
    if common.empty:
        return pd.Series(dtype=float)
    fr = factor_values.loc[common].rank(axis=1)
    rr = fwd_returns.loc[common].rank(axis=1)
    # Require at least 3 symbols with data on a date for a meaningful rank corr.
    enough = (factor_values.loc[common].notna() & fwd_returns.loc[common].notna()).sum(axis=1) >= 3
    ic = fr.corrwith(rr, axis=1)
    ic[~enough] = np.nan
    return ic.dropna()


def icir(ic_series: pd.Series, *, std_floor: float = 1e-3) -> float:
    """IC information ratio: mean(IC) / std(IC).

    The denominator is floored at ``std_floor`` so a stable, non-zero-mean IC
    series (very low dispersion) yields a high — not zero — ratio. Real factors
    have IC std well above the floor, so it only matters for degenerate inputs.
    """
    if len(ic_series) < 2:
        return 0.0
    mean = float(ic_series.mean())
    std = float(ic_series.std())
    if np.isnan(std) or np.isnan(mean):
        return 0.0
    return mean / max(std, std_floor)


def turnover(factor_values: pd.DataFrame) -> float:
    """Average day-over-day fractional change in cross-sectional rank."""
    ranks = factor_values.rank(axis=1)
    n = ranks.notna().sum(axis=1).replace(0, np.nan)
    daily = ranks.diff().abs().sum(axis=1) / n
    daily = daily.dropna()
    if daily.empty:
        return 0.0
    return float(daily.mean())


def decay_halflife_days(ic_series: pd.Series) -> float | None:
    """Estimate IC decay half-life from lag-1 autocorrelation (AR(1) model).

    ``IC_t ~ rho * IC_{t-1}`` -> half-life = ln(0.5) / ln(rho). Returns ``None``
    when ``rho`` is non-positive or >= 1 (no exponential decay to speak of).
    """
    if len(ic_series) < 4:
        return None
    rho = ic_series.autocorr(lag=1)
    if rho is None or np.isnan(rho) or rho <= 0 or rho >= 1:
        return None
    return float(np.log(0.5) / np.log(rho))


def _auto_download_prices(
    db: Session,
    universe: list[str],
    start: datetime,
    end: datetime,
) -> None:
    """Download price data for ``universe`` symbols when ``bar_prices`` is empty.

    Uses the provider chain (OpenBB → Alpha Vantage → Finnhub → yfinance) via
    :class:`DataIngester` to populate the ``bar_prices`` table so the miner can
    build a non-empty panel on a fresh database.
    """
    # Build the ingester outside-in so the provider registry can be constructed
    # with the current db session.
    try:
        ingester = DataIngester(db)
    except Exception as exc:
        logger.warning("Miner: could not create DataIngester for auto-download: %s", exc)
        return

    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    for symbol in universe:
        try:
            result = ingester.ingest_bar_prices(
                symbol, start_date=start_str, end_date=end_str
            )
            if result.get("success"):
                logger.info(
                    "Miner: auto-downloaded %s rows for %s from %s",
                    result.get("rows_inserted", 0),
                    symbol,
                    result.get("provider", "unknown"),
                )
            else:
                logger.debug(
                    "Miner: auto-download failed for %s: %s",
                    symbol,
                    result.get("message"),
                )
        except Exception as exc:
            logger.debug("Miner: auto-download exception for %s: %s", symbol, exc)


def _factor_values(
    panel: dict[str, pd.DataFrame],
    *,
    name: str | None = None,
    dsl: str | None = None,
) -> pd.DataFrame:
    """Compute a factor's (date x symbol) values from the panel.

    DSL formulas go through the sandbox; seed factors are computed per-symbol via
    :func:`quant_factors.compute_factor`.
    """
    if dsl:
        return factor_dsl.evaluate(dsl, panel)

    if not name:
        raise ValueError("either name or dsl is required")

    symbols = list(panel["close"].columns)
    series_by_symbol: dict[str, pd.Series] = {}
    for sym in symbols:
        sym_df = pd.DataFrame(
            {var: panel[var][sym] for var in panel if sym in panel[var].columns}
        )
        series_by_symbol[sym] = quant_factors.compute_factor(name, sym_df)
    return pd.DataFrame(series_by_symbol)


def factor_values(
    panel: dict[str, pd.DataFrame],
    *,
    name: str | None = None,
    dsl: str | None = None,
) -> pd.DataFrame:
    """A factor's (date x symbol) values on *panel*; the scorer-facing name
    for :func:`_factor_values` (Discover's exposure stage uses it)."""
    return _factor_values(panel, name=name, dsl=dsl)


def evaluate_candidate(
    panel: dict[str, pd.DataFrame],
    *,
    name: str,
    formula: str,
    source: str,
    dsl: str | None,
    horizon: int,
    min_ic: float,
    min_icir: float,
) -> FactorCandidate:
    """Compute metrics for one factor and wrap them in a :class:`FactorCandidate`."""
    try:
        values = _factor_values(panel, name=name if dsl is None else None, dsl=dsl)
        fwd = forward_returns(panel["close"], horizon)
        # Align factor values to the panel grid before correlating.
        values = values.reindex(index=panel["close"].index, columns=panel["close"].columns)
        ic_series = cross_sectional_ic(values, fwd)
        ic_mean = float(ic_series.mean()) if not ic_series.empty else 0.0
        ic_ir = icir(ic_series)
        to = turnover(values)
        hl = decay_halflife_days(ic_series)
        is_valid = abs(ic_mean) > min_ic and abs(ic_ir) > min_icir
        return FactorCandidate(
            name=name,
            formula=formula,
            source=source,
            ic=ic_mean,
            icir=ic_ir,
            turnover=to,
            decay_halflife_days=hl,
            valid=is_valid,
            validation_notes=f"IC={ic_mean:.4f}, ICIR={ic_ir:.2f}, TO={to:.4f}, n={len(ic_series)}",
            dsl=dsl,
            ic_series=[float(x) for x in ic_series.tolist()],
        )
    except Exception as exc:  # invalid formula / computation error -> not valid
        logger.debug("Miner: factor %s failed: %s", name, exc)
        return FactorCandidate(
            name=name,
            formula=formula,
            source=source,
            ic=0.0,
            icir=0.0,
            turnover=0.0,
            valid=False,
            validation_notes=f"Computation error: {exc}",
            dsl=dsl,
        )


def persist_candidate(db: Session, candidate: FactorCandidate) -> str:
    """Upsert a candidate into ``FactorsLibrary`` (by name, among active rows).

    Returns the row id. Updating in place keeps the library stable across daily
    runs instead of accumulating a duplicate row per run.
    """
    formula_json = json.dumps(
        {"formula": candidate.formula, "dsl": candidate.dsl, "name": candidate.name}
    )
    ic_summary_json = json.dumps(
        {
            "ic": candidate.ic,
            "icir": candidate.icir,
            "turnover": candidate.turnover,
            "decay_halflife_days": candidate.decay_halflife_days,
            "ic_series": candidate.ic_series,
            "n_obs": len(candidate.ic_series),
            "computed_at": datetime.now(UTC).isoformat(),
        }
    )

    existing = db.execute(
        select(FactorsLibrary).where(
            FactorsLibrary.name == candidate.name,
            FactorsLibrary.retired_at.is_(None),
        )
    ).scalars().first()

    if existing is not None:
        existing.formula_json = formula_json
        existing.source = candidate.source
        existing.ic_summary_json = ic_summary_json
        db.commit()
        candidate.id = existing.id
        return existing.id

    row = FactorsLibrary(
        name=candidate.name,
        formula_json=formula_json,
        source=candidate.source,
        ic_summary_json=ic_summary_json,
    )
    db.add(row)
    db.commit()
    candidate.id = row.id
    return row.id


async def run_miner(
    db: Session,
    universe: list[str],
    lookback_years: float = 5.0,
    *,
    horizon: int | None = None,
    registry=None,
    extra_factors: list[dict] | None = None,
    propose_llm: bool = False,
    regime_label: str | None = None,
) -> list[FactorCandidate]:
    """Run the Miner: compute real metrics over ``universe`` and persist survivors.

    Args:
        db: Database session.
        universe: Index basket forming the IC cross-section.
        lookback_years: Price history window.
        horizon: Forward-return horizon in days (defaults to setting/5).
        registry: Provider registry override (for tests).
        extra_factors: Additional factor defs, each ``{name, formula, source, dsl}``
            (used to inject validated LLM proposals).

    Returns:
        Candidates sorted by |ICIR| descending. Valid survivors carry a real
        ``FactorsLibrary`` id.
    """
    settings = get_public_settings(db)
    horizon = horizon or int(settings.get("alphacrafter_ic_horizon_days", DEFAULT_HORIZON_DAYS))
    min_ic = float(settings.get("alphacrafter_min_ic", DEFAULT_MIN_IC))
    min_icir = float(settings.get("alphacrafter_min_icir", DEFAULT_MIN_ICIR))

    end = datetime.now(UTC)
    start = end - pd.Timedelta(days=int(lookback_years * 365))
    if not isinstance(start, datetime):
        start = end - timedelta(days=int(lookback_years * 365))
    panel = build_panel(db, universe, start, end, registry=registry)

    candidates: list[FactorCandidate] = []
    if not panel:
        logger.warning("Miner: empty panel for universe %s — attempting auto-download", universe)
        _auto_download_prices(db, universe, start, end)
        panel = build_panel(db, universe, start, end, registry=registry)
        if not panel:
            logger.warning("Miner: still empty after auto-download for %s", universe)
            return candidates

    factor_defs: list[dict] = [
        {
            "name": d.get("name"),
            "formula": d.get("formula", ""),
            "source": d.get("source", "quant_factors"),
            "dsl": d.get("dsl"),
        }
        for d in quant_factors.get_factor_definitions()
    ]
    # 1. Deterministic seed factors first
    for d in factor_defs:
        candidate = evaluate_candidate(
            panel,
            name=d["name"],
            formula=d["formula"],
            source=d["source"],
            dsl=d.get("dsl"),
            horizon=horizon,
            min_ic=min_ic,
            min_icir=min_icir,
        )
        if candidate.valid:
            persist_candidate(db, candidate)
        candidates.append(candidate)

    # 2. LLM extra proposals (best-effort, fail-open)
    merged_extra = list(extra_factors or [])
    if propose_llm:
        try:
            llm_extras = await asyncio.wait_for(
                _llm_extra_factors(db, panel, regime_label, settings),
                timeout=45.0,
            )
            merged_extra.extend(llm_extras)
        except Exception as exc:
            logger.warning("Miner: LLM proposal step timed out or failed (non-fatal): %s", exc)

    for extra in merged_extra:
        candidate = evaluate_candidate(
            panel,
            name=extra.get("name", "unknown_extra"),
            formula=extra.get("formula", ""),
            source=extra.get("source", "llm"),
            dsl=extra.get("dsl"),
            horizon=horizon,
            min_ic=min_ic,
            min_icir=min_icir,
        )
        if candidate.valid:
            persist_candidate(db, candidate)
        candidates.append(candidate)

    candidates.sort(key=lambda c: abs(c.icir), reverse=True)
    return candidates


async def _llm_extra_factors(
    db: Session,
    panel: dict[str, pd.DataFrame],
    regime_label: str | None,
    settings: dict,
) -> list[dict]:
    """Fetch DSL-validated LLM factor proposals as Miner ``extra_factors``.

    Imported lazily so the (heavy) LLM/RAG stack is not pulled in unless LLM
    proposals are actually requested. Best-effort: returns ``[]`` on any failure.
    """
    try:
        from app.lab.alphacrafter.llm_factor_proposer import (
            propose_factors_via_llm,
            proposals_to_factor_defs,
        )

        force_local = bool(settings.get("llm_force_local", False))
        n_days = int(panel["close"].shape[0]) if "close" in panel else 0
        n_stocks = int(panel["close"].shape[1]) if "close" in panel else 0
        proposals, _ = await propose_factors_via_llm(
            db,
            universe_stats={"n_stocks": n_stocks, "n_days": n_days},
            regime_label=regime_label,
            force_local=force_local,
        )
        return proposals_to_factor_defs(proposals)
    except Exception as exc:  # pragma: no cover - best-effort enrichment
        logger.warning("Miner: LLM proposal step failed: %s", exc)
        return []
