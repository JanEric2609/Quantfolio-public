"""Quarterly walk-forward tuning of AlphaCrafter trader configurations.

Selects the trader sweep configuration (rebalance frequency × position size ×
commission) that is most likely to persist out-of-sample, guarded against
backtest overfitting. Every candidate combination is evaluated with strictly
chronological walk-forward splits and logged as an ``AlphacrafterTuningTrial``
row; only configurations passing all statistical gates are eligible for
selection, and the final 12 months of data are held out and touched exactly
once, after selection.

Formulas and citations
----------------------
- **Deflated Sharpe Ratio (DSR)**: Bailey & López de Prado, "The Deflated
  Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and
  Non-Normality", *Journal of Portfolio Management* 40(5), 2014. DSR is PSR
  evaluated at the expected maximum Sharpe under ``n_trials``:
  ``E[max] = σ · ((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)))`` with γ = Euler–Mascheroni.
- **IC-Sharpe analog** (:func:`deflated_ic_sharpe`): the same construction
  applied to the *information coefficient* series instead of strategy returns.
  This is deliberately a SEPARATE function from
  ``quant_metrics.deflated_sharpe_ratio`` (the canonical returns-based DSR,
  reused wherever a returns series exists): per-config evaluation here yields
  cross-sectional IC values — bounded in [−1, 1], with a different null
  distribution and no annualisation step — so feeding them into a
  returns-based Sharpe estimator would misstate both the moment scaling and
  the deflation threshold. The expected-maximum term is reused verbatim from
  ``quant_metrics.expected_max_sharpe`` (the formula is ratio-agnostic).
- **Purge / embargo**: López de Prado, *Advances of Financial Machine
  Learning*, Wiley 2018, ch. 7 — remove ``purge_days ≥ ic_horizon``
  observations around each test block (leakage through overlapping
  forward-return windows) and embargo ≈ 1% of the sample after each block.
- **Multiple-testing threshold**: Harvey, Liu & Zhu, "…and the Cross-Section
  of Expected Returns", *Review of Financial Studies* 29(1), 2016 — require
  t > 3.0 for newly mined signals given the size of the factor zoo.
- **Walk-forward efficiency**: Pardo, *The Evaluation and Optimization of
  Trading Strategies*, Wiley 2008 — WFE = OOS performance / IS performance;
  WFE ≥ 0.5 is the hard gate below.

Selection rules enforced in code (:func:`select_config`):
OOS min-IC and min-ICIR gates plus a Newey-West lagged t-stat (lags ≥ IC
horizon; Bartlett kernel) compared against the HLZ t > 3.0 threshold; plateau
rule (all ±1-step grid neighbours must also pass, else the centre of the
largest connected passing plateau wins); median-of-folds consensus parameter
vector recorded alongside; WFE ≥ 0.5 hard gate; terminal holdout evaluated
once post-selection. If zero candidates pass, the job records a
"no acceptable configuration" result and leaves every setting untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.foundation.models.entities import (
    AlphacrafterJobRun,
    AlphacrafterTuningTrial,
    FactorsLibrary,
    ScreenerRun,
)
from app.lab.alphacrafter.miner import cross_sectional_ic, forward_returns, icir
from app.lab.alphacrafter.panel import build_panel
from app.lab.alphacrafter.trader import (
    TraderConfig,
    _generate_config_grid,
    _rebalance_dates,
    _trader_grid_settings,
    composite_score,
)
# Re-imported for backward compatibility — moved to quant_metrics.py
# (2026-08-27, foundation-tier, no decision-loop deps) so any consumer can
# reuse them without importing through the alphacrafter facade.
from app.foundation.quant_metrics import (
    HLZ_T_THRESHOLD,
    deflated_ic_sharpe,
    newey_west_t_stat,
    record_trial,
)
from app.foundation.settings import get_public_settings

logger = logging.getLogger(__name__)

#: Terminal holdout length in trading days (~12 months); never searched.
HOLDOUT_TRADING_DAYS = 252

#: Hard cap on tuned configurations per run (mirrors orchestrator.MAX_SWEEP_CONFIGS).
MAX_TUNING_CONFIGS = 12

#: Pardo walk-forward efficiency hard gate.
MIN_WFE = 0.5


# ---------------------------------------------------------------------------
# Walk-forward splits (AFML ch. 7: purge + embargo, strictly chronological)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkForwardFold:
    """One expanding-window fold over positional row indices."""

    fold: int
    train_positions: np.ndarray
    test_positions: np.ndarray

    @property
    def train_end(self) -> int:
        return int(self.train_positions[-1]) if len(self.train_positions) else -1

    @property
    def test_start(self) -> int:
        return int(self.test_positions[0])

    @property
    def test_end(self) -> int:
        return int(self.test_positions[-1])


def _panel_row_count(panel: pd.DataFrame | int) -> int:
    return panel if isinstance(panel, int) else len(panel)


def walk_forward_splits(
    panel: pd.DataFrame | int,
    *,
    n_folds: int = 5,
    purge_days: int = 5,
    embargo_frac: float = 0.01,
) -> list[WalkForwardFold]:
    """Chronological expanding-window splits with purge and embargo gaps.

    Args:
        panel: Panel DataFrame (rows are chronological observations) or a row count.
        n_folds: Number of contiguous test blocks; must be ≥ 5.
        purge_days: Rows dropped between each train prefix and its test block
            (≥ forward-return horizon so labels never overlap the train set).
        embargo_frac: Fraction of the sample embargoed after each test block;
            those rows may never re-enter any later fold's training data.

    Returns:
        Folds in strict chronological order; test blocks are disjoint and
        cover the sample, each training set is a purge/embargo-filtered
        prefix ending before its test block.
    """
    n = _panel_row_count(panel)
    if n_folds < 5:
        raise ValueError(f"walk_forward_splits requires n_folds >= 5, got {n_folds}")
    # One extra leading block seeds training so even fold 0 has history.
    total_blocks = n_folds + 1
    if n < total_blocks * max(purge_days + 1, 10):
        raise ValueError(f"panel too small ({n} rows) for {n_folds}-fold walk-forward")

    embargo_days = max(1, int(round(embargo_frac * n)))
    bounds = [round(i * n / total_blocks) for i in range(total_blocks + 1)]

    folds: list[WalkForwardFold] = []
    embargo_mask = np.zeros(n, dtype=bool)
    for i in range(n_folds):
        test_start, test_end = bounds[i + 1], bounds[i + 2]
        train_limit = max(test_start - purge_days, 0)
        positions = np.arange(train_limit)
        folds.append(
            WalkForwardFold(
                fold=i,
                train_positions=positions[~embargo_mask[:train_limit]],
                test_positions=np.arange(test_start, test_end),
            )
        )
        # Embargo applies to LATER folds only (AFML ch. 7): rows right after a
        # test block carry leaked label information for subsequent training.
        embargo_mask[test_end:test_end + embargo_days] = True
    return folds


def split_search_holdout(n_rows: int, holdout_days: int = HOLDOUT_TRADING_DAYS) -> tuple[int, int]:
    """Split row count into ``(search_rows, holdout_rows)``; holdout goes last."""
    holdout_rows = min(holdout_days, max(n_rows // 3, 0))
    return n_rows - holdout_rows, holdout_rows


# ---------------------------------------------------------------------------
# Per-config evaluation → stitched-OOS IC series
# ---------------------------------------------------------------------------


@dataclass
class ConfigEvaluation:
    """Walk-forward evaluation of one trader configuration."""

    config: TraderConfig
    is_mean_ic: float
    oos_mean_ic: float
    oos_icir: float
    wfe: float
    oos_ic_series: list[float] = field(default_factory=list)
    per_fold_oos_mean_ic: list[float] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, float, float]:
        return (self.config.rebalance_freq, self.config.position_size, self.config.commission)


def _config_signal_frame(composite: pd.DataFrame, config: TraderConfig) -> pd.DataFrame:
    """Composite restricted to each rebalance date's top ``max_positions`` names.

    Restricting the IC sample to the config's rebalance cadence and holding
    breadth is what makes the evaluation config-dependent (frequency changes
    the sampled dates, breadth the cross-section); commission and position
    size are execution parameters carried on the trial ledger but they do not
    alter signal quality.
    """
    signal = pd.DataFrame(np.nan, index=composite.index, columns=composite.columns)
    for d in _rebalance_dates(composite.index, config.rebalance_freq):
        if d not in composite.index:
            continue
        row = composite.loc[d]
        top = row.dropna().sort_values(ascending=False).head(config.max_positions).index
        signal.loc[d, top] = row.loc[top].values
    return signal


def evaluate_config(
    close: pd.DataFrame,
    composite: pd.DataFrame,
    config: TraderConfig,
    folds: list[WalkForwardFold],
    horizon: int,
) -> ConfigEvaluation:
    """Evaluate one config across all folds; stitch per-fold OOS IC series."""
    fwd = forward_returns(close, horizon)
    signal = _config_signal_frame(composite, config)
    full_ic = cross_sectional_ic(signal, fwd)

    def _ic_at(positions: np.ndarray) -> np.ndarray:
        if len(positions) == 0:
            return np.array([])
        dates = [close.index[p] for p in positions if p < len(close.index)]
        return full_ic.reindex(dates).dropna().to_numpy(dtype=float)

    is_parts: list[np.ndarray] = []
    oos_parts: list[np.ndarray] = []
    per_fold_oos: list[float] = []
    for fold in folds:
        is_vals = _ic_at(fold.train_positions)
        oos_vals = _ic_at(fold.test_positions)
        is_parts.append(is_vals)
        oos_parts.append(oos_vals)
        per_fold_oos.append(float(oos_vals.mean()) if len(oos_vals) else 0.0)

    is_series = np.concatenate(is_parts) if is_parts else np.array([])
    oos_series = np.concatenate(oos_parts) if oos_parts else np.array([])

    is_mean = float(is_series.mean()) if len(is_series) else 0.0
    oos_mean = float(oos_series.mean()) if len(oos_series) else 0.0
    oos_icir_val = float(icir(pd.Series(oos_series))) if len(oos_series) >= 2 else 0.0

    # Pardo WFE on the IC level; a non-positive IS mean cannot yield a
    # meaningful efficiency and always fails the >= 0.5 gate.
    if len(is_series) >= 2 and is_mean > 1e-9:
        wfe = oos_mean / is_mean
    else:
        wfe = 0.0

    return ConfigEvaluation(
        config=config,
        is_mean_ic=is_mean,
        oos_mean_ic=oos_mean,
        oos_icir=oos_icir_val,
        wfe=wfe,
        oos_ic_series=[float(x) for x in oos_series],
        per_fold_oos_mean_ic=per_fold_oos,
    )


def passes_gates(
    ev: ConfigEvaluation,
    *,
    min_ic: float,
    min_icir: float,
    horizon: int,
    t_threshold: float = HLZ_T_THRESHOLD,
) -> bool:
    """All statistical acceptance gates for one configuration.

    OOS mean IC ≥ min_ic, OOS ICIR ≥ min_icir, Newey-West t-stat on the
    stitched OOS IC series (lags ≥ horizon) above the HLZ t > 3.0 threshold,
    and the Pardo WFE ≥ 0.5 hard gate.
    """
    if ev.oos_mean_ic < min_ic:
        return False
    if ev.oos_icir < min_icir:
        return False
    if ev.wfe < MIN_WFE:
        return False
    t_stat = newey_west_t_stat(ev.oos_ic_series, lags=max(horizon, 1))
    return t_stat > t_threshold


# ---------------------------------------------------------------------------
# Selection: plateau rule + median-of-folds consensus
# ---------------------------------------------------------------------------


@dataclass
class SelectionResult:
    selected: ConfigEvaluation | None
    accepted_evals: list[ConfigEvaluation]
    basis: str  # "strict_plateau" | "largest_plateau_center" | "none"
    consensus_config: dict | None
    reason: str


def _grid_axes(grid: list[TraderConfig]) -> tuple[list[str], list[float], list[float]]:
    freqs = list(dict.fromkeys(c.rebalance_freq for c in grid))
    sizes = sorted({c.position_size for c in grid})
    comms = sorted({c.commission for c in grid})
    return freqs, sizes, comms


def _axis_neighbors(config: TraderConfig, grid: list[TraderConfig]) -> list[tuple[str, float, float]]:
    freqs, sizes, comms = _grid_axes(grid)
    neighbors: list[tuple[str, float, float]] = []
    fi = freqs.index(config.rebalance_freq)
    si = sizes.index(config.position_size)
    ci = comms.index(config.commission)
    for df, ds, dc in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        nf, ns, nc = fi + df, si + ds, ci + dc
        if 0 <= nf < len(freqs) and 0 <= ns < len(sizes) and 0 <= nc < len(comms):
            neighbors.append((freqs[nf], sizes[ns], comms[nc]))
    return neighbors


def _largest_plateau_center(passing: list[ConfigEvaluation], grid: list[TraderConfig]) -> ConfigEvaluation:
    """Centre of the largest axis-connected passing component.

    Connectivity steps are ±1 along the FULL grid axes; centre = member
    minimising total graph distance to its component, ties break on higher
    OOS ICIR.
    """
    keys = {ev.key: ev for ev in passing}
    adjacency: dict[tuple, set[tuple]] = {k: set() for k in keys}
    for key, ev in keys.items():
        for nb in _axis_neighbors(ev.config, grid):
            if nb in keys:
                adjacency[key].add(nb)

    seen: set[tuple] = set()
    best_component: list[tuple] = []
    for key in keys:
        if key in seen:
            continue
        stack, component = [key], []
        seen.add(key)
        while stack:
            cur = stack.pop()
            component.append(cur)
            for nb in adjacency[cur]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        if len(component) > len(best_component):
            best_component = component

    def centrality(k: tuple) -> tuple[int, float]:
        dists = _bfs_distances(adjacency, k, best_component)
        total = sum(dists.values())
        return (total, -keys[k].oos_icir)

    center_key = min(best_component, key=centrality)
    return keys[center_key]


def _bfs_distances(adjacency, start, component) -> dict[tuple, int]:
    from collections import deque

    dists = {start: 0}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        for nb in adjacency[cur]:
            if nb not in dists:
                dists[nb] = dists[cur] + 1
                queue.append(nb)
    return {k: v for k, v in dists.items() if k in set(component)}


def median_consensus_config(
    evaluations: list[ConfigEvaluation],
    grid: list[TraderConfig],
) -> dict:
    """Median-of-folds consensus parameter vector, snapped to the grid.

    Per fold the winning config is the one with the highest mean OOS IC in
    that fold; the consensus takes the mode frequency and median numeric
    parameters across folds, then snaps to the nearest actual grid entry so
    the reported vector is executable.
    """
    n_folds = max((len(ev.per_fold_oos_mean_ic) for ev in evaluations), default=0)
    if n_folds == 0 or not evaluations:
        return {}
    winners: list[TraderConfig] = []
    for f in range(n_folds):
        best = max(evaluations, key=lambda ev: ev.per_fold_oos_mean_ic[f])
        winners.append(best.config)
    freq = Counter(w.rebalance_freq for w in winners).most_common(1)[0][0]
    med_size = float(np.median([w.position_size for w in winners]))
    med_comm = float(np.median([w.commission for w in winners]))

    grid_freqs = list(dict.fromkeys(c.rebalance_freq for c in grid))

    def distance(c: TraderConfig) -> tuple[int, float, float]:
        return (
            abs(grid_freqs.index(c.rebalance_freq) - grid_freqs.index(freq)),
            abs(c.position_size - med_size),
            abs(c.commission - med_comm),
        )

    nearest = min(grid, key=distance)
    return asdict(nearest)


def select_config(
    evaluations: list[ConfigEvaluation],
    grid: list[TraderConfig],
    *,
    min_ic: float,
    min_icir: float,
    horizon: int,
    t_threshold: float = HLZ_T_THRESHOLD,
) -> SelectionResult:
    """Plateau-rule selection over gate-passing configurations."""
    passing = [ev for ev in evaluations if passes_gates(
        ev, min_ic=min_ic, min_icir=min_icir, horizon=horizon, t_threshold=t_threshold,
    )]
    if not passing:
        return SelectionResult(
            selected=None,
            accepted_evals=[],
            basis="none",
            consensus_config=None,
            reason="No acceptable configuration found — existing settings left unchanged.",
        )

    passing_keys = {ev.key for ev in passing}
    strict_plateau = [
        ev for ev in passing
        if all(nb in passing_keys for nb in _axis_neighbors(ev.config, grid))
    ]
    if strict_plateau:
        chosen = max(strict_plateau, key=lambda ev: ev.oos_icir)
        basis = "strict_plateau"
    else:
        chosen = _largest_plateau_center(passing, grid)
        basis = "largest_plateau_center"

    consensus = median_consensus_config(passing, grid)
    return SelectionResult(
        selected=chosen,
        accepted_evals=passing,
        basis=basis,
        consensus_config=consensus,
        reason=f"{len(passing)} of {len(evaluations)} candidates passed all gates.",
    )


# ---------------------------------------------------------------------------
# Job entry points
# ---------------------------------------------------------------------------


def _resolve_universe(db: Session, universe: list[str] | None) -> list[str]:
    from app.lab.alphacrafter.universe import resolve_miner_universe  # noqa: PLC0415

    return resolve_miner_universe(get_public_settings(db), universe)


def _load_factors(db: Session) -> list[FactorsLibrary]:
    latest_screener = db.execute(
        select(ScreenerRun).order_by(ScreenerRun.ts.desc()).limit(1)
    ).scalars().first()
    if latest_screener and latest_screener.selected_factor_ids:
        ids = [x for x in latest_screener.selected_factor_ids.split(",") if x]
        if ids:
            factors = list(
                db.execute(select(FactorsLibrary).where(FactorsLibrary.id.in_(ids))).scalars().all()
            )
            if factors:
                return factors
    return list(
        db.execute(
            select(FactorsLibrary).where(FactorsLibrary.retired_at.is_(None))
        ).scalars().all()
    )


async def run_tuning_job(
    db: Session,
    job_run_id: str | None = None,
    universe: list[str] | None = None,
    *,
    registry=None,
) -> dict:
    """Quarterly walk-forward tuning job body.

    Evaluates every grid configuration with purge/embargo walk-forward splits,
    logs EVERY trial to ``AlphacrafterTuningTrial``, selects via the plateau
    rule, evaluates the terminal holdout once post-selection, and writes the
    outcome to the job run's ``result_json``. Never mutates settings: the
    selected configuration is recorded for review, not auto-applied.

    When *job_run_id* is None a fresh ``AlphacrafterJobRun`` row is created so
    trial rows always reference an existing job (FK integrity).
    """
    from uuid import uuid4  # noqa: PLC0415

    if job_run_id is None:
        job_run_id = uuid4().hex
        db.add(AlphacrafterJobRun(
            id=job_run_id,
            status="running",
            progress_json=json.dumps({"stages": {"tuning": {"stage": "tuning", "status": "running"}}}),
        ))
        db.commit()

    def _finish(status: str, result: dict) -> dict:
        row = db.get(AlphacrafterJobRun, job_run_id)
        if row is not None:
            row.status = status
            row.result_json = json.dumps(result)
            row.completed_at = datetime.now(UTC)
            db.commit()
        return result

    settings = get_public_settings(db)
    horizon = int(settings.get("alphacrafter_ic_horizon_days", 5))
    min_ic = float(settings.get("alphacrafter_min_ic", 0.02))
    min_icir = float(settings.get("alphacrafter_min_icir", 0.3))

    symbols = _resolve_universe(db, universe)
    end = datetime.now(UTC)
    start = end - timedelta(days=int(5 * 365))

    panel = build_panel(db, symbols, start, end, registry=registry)
    if not panel or "close" not in panel or panel["close"].empty:
        return _finish("completed", {
            "job_type": "alphacrafter_tuning",
            "message": "No price panel available for tuning — run the pipeline first.",
            "n_trials": 0,
        })

    close = panel["close"]
    search_rows, holdout_rows = split_search_holdout(len(close))
    if search_rows < 250:
        return _finish("completed", {
            "job_type": "alphacrafter_tuning",
            "message": f"Not enough history ({len(close)} rows) for walk-forward tuning.",
            "n_trials": 0,
        })

    factors = _load_factors(db)
    if not factors:
        return _finish("completed", {
            "job_type": "alphacrafter_tuning",
            "message": "No factors available for tuning — run the miner first.",
            "n_trials": 0,
        })

    composite = composite_score(panel, factors)
    if composite.dropna(how="all").empty:
        return _finish("completed", {
            "job_type": "alphacrafter_tuning",
            "message": "Composite score empty — no computable factors.",
            "n_trials": 0,
        })

    freqs, sizes, comms, max_positions = _trader_grid_settings(settings)
    grid = _generate_config_grid(freqs, sizes, comms, max_positions=max_positions)
    if len(grid) > MAX_TUNING_CONFIGS:
        logger.warning(
            "Tuning: settings-driven grid yields %d configs — truncating to cap %d",
            len(grid), MAX_TUNING_CONFIGS,
        )
        grid = grid[:MAX_TUNING_CONFIGS]

    folds = walk_forward_splits(
        search_rows,
        n_folds=5,
        purge_days=max(horizon, 1),
        embargo_frac=0.01,
    )

    evaluations: list[ConfigEvaluation] = []
    for config in grid:
        ev = evaluate_config(close.iloc[:search_rows], composite.iloc[:search_rows], config, folds, horizon)
        evaluations.append(ev)
        db.add(AlphacrafterTuningTrial(
            job_run_id=job_run_id,
            config_json=asdict(config),
            is_mean_ic=ev.is_mean_ic,
            oos_icir=ev.oos_icir,
            wfe=ev.wfe,
            dsr=deflated_ic_sharpe(ev.oos_ic_series, n_trials=len(grid)),
            accepted=False,
        ))
        # First writer to the global trial ledger (ADR 0015 disposition):
        # every grid config evaluated here is one independent hypothesis
        # test, counted globally by quant_metrics.resolve_n_trials regardless
        # of which context later reads it. Scoped to this job_run_id so a
        # retried run doesn't double-count, but a NEW tuning run (fresh data)
        # legitimately adds new trials — repeated search is still search.
        record_trial(
            db,
            context="alphacrafter_tuning",
            trial_key=f"{job_run_id}:{config.rebalance_freq}_{config.position_size}_{config.commission}",
            metadata={"job_run_id": job_run_id, "config": asdict(config)},
        )
    db.commit()

    selection = select_config(evaluations, grid, min_ic=min_ic, min_icir=min_icir, horizon=horizon)

    holdout_payload: dict = {}
    accepted_config: dict | None = None
    if selection.selected is not None:
        accepted_config = asdict(selection.selected.config)
        chosen_key = selection.selected.key
        for trial in db.execute(select(AlphacrafterTuningTrial)).scalars().all():
            cfg = trial.config_json or {}
            if (cfg.get("rebalance_freq"), cfg.get("position_size"), cfg.get("commission")) == chosen_key:
                trial.accepted = True
        db.commit()

        # Terminal holdout: final 12 months, evaluated ONCE post-selection.
        holdout_close = close.iloc[search_rows:]
        holdout_composite = composite.iloc[search_rows:]
        holdout_ev = evaluate_config(
            holdout_close,
            holdout_composite,
            selection.selected.config,
            [WalkForwardFold(
                fold=0,
                train_positions=np.array([]),
                test_positions=np.arange(len(holdout_close)),
            )],
            horizon,
        )
        holdout_payload = {
            "mean_ic": holdout_ev.oos_mean_ic,
            "t_stat": newey_west_t_stat(holdout_ev.oos_ic_series, lags=max(horizon, 1)),
            "n_obs": len(holdout_ev.oos_ic_series),
        }

    result = {
        "job_type": "alphacrafter_tuning",
        "universe": symbols,
        "n_trials": len(evaluations),
        "accepted_config": accepted_config,
        "selection_basis": selection.basis,
        "consensus_config": selection.consensus_config,
        "holdout": holdout_payload,
        "message": (
            f"Tuning complete: {selection.reason} Selected "
            f"{accepted_config} (basis={selection.basis})."
            if accepted_config else selection.reason
        ),
    }
    return _finish("completed", result)


def run_tuning_job_sync(job_run_id: str | None = None) -> dict:
    """Synchronous wrapper for the cron scheduler and submit_job.

    Creates its own DB session; ``run_tuning_job`` creates the auditable
    ``AlphacrafterJobRun`` row itself when *job_run_id* is None (cron path).
    """
    from app.foundation.core.db import SessionLocal  # noqa: PLC0415

    db = SessionLocal()
    try:
        return asyncio.run(run_tuning_job(db, job_run_id))
    except Exception:
        logger.exception("AlphaCrafter tuning job failed")
        return {"job_type": "alphacrafter_tuning", "error": "internal error"}
    finally:
        db.close()
