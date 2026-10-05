"""AlphaCrafter Trader: factor strategy backtest sweep (Phase 4).

Turns the Screener's selected factors into a long-only strategy and sweeps
rebalance frequency / position sizing / commission, scoring each config with a
**real vectorbt backtest** (no random numbers).

Per config the pipeline is:

1. Load the factors the Screener selected (by ``screener_run_id``).
2. Build a (date × symbol) price+fundamentals panel from ``BarStore`` (via
   :func:`alphacrafter.panel.build_panel`).
3. Compute each factor's cross-sectional values (same code path as the Miner),
   z-score per day, orient/weight by the factor's IC, and average into one
   **composite score** per (date × symbol).
4. At each rebalance date go long the top ``max_positions`` names; size each at
   ``position_size`` (capped to 100% gross); forward-fill between rebalances.
5. Build the strategy equity curve from real asset returns net of turnover-based
   transaction costs, then hand that single series to
   :func:`backtest_vbt.engine.run_backtest` so vectorbt computes Sharpe / max
   drawdown / return on the actual factor-driven curve.

Configs are ranked by Sharpe; every run is persisted to ``TraderBacktest``.
"""

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import cast

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.foundation.models.entities import FactorsLibrary, ScreenerRun, TraderBacktest
from app.lab.alphacrafter.miner import _factor_values
from app.lab.alphacrafter.panel import build_panel
from app.lab.alphacrafter.shared_memory import SharedMemoryH
from app.lab.backtest_vbt.engine import BacktestSpec, run_backtest
from app.lab.backtest_vbt.walk_forward import walk_forward_test
from app.foundation.settings import get_public_settings

logger = logging.getLogger(__name__)

DEFAULT_REBALANCE_FREQS = ["W", "M"]
DEFAULT_POSITION_SIZES = [0.05, 0.1, 0.2]
DEFAULT_COMMISSIONS = [0.001]
DEFAULT_MAX_POSITIONS = 5

#: Pardo walk-forward efficiency hard gate (OOS Sharpe / IS Sharpe), applied
#: as a real pre-selection gate before a config is promoted into the ranked
#: results (Track C1). Mirrors ``alphacrafter.tuning.MIN_WFE`` exactly — not
#: imported from there to avoid a circular import (tuning.py already imports
#: grid helpers FROM this module).
MIN_WFE = 0.5


def _parse_str_list(value: object, default: list[str]) -> list[str]:
    """Parse a settings value into a non-empty list of strings.

    Accepts CSV strings (``"W,M"``), JSON lists of scalars, or bare scalars.
    Empty/malformed input falls back to ``default`` with a warning — never a
    validation error (legacy stored values must keep working).
    """
    try:
        if value is None:
            return list(default)
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",") if p.strip()]
        elif isinstance(value, (list, tuple)):
            parts = [str(p).strip() for p in value if str(p).strip()]
        else:
            part = str(value).strip()
            parts = [part] if part else []
        parsed = parts or list(default)
        return parsed
    except Exception:
        logger.warning(
            "Trader: malformed string-list setting %r — falling back to default %s",
            value, default,
        )
        return list(default)


def _parse_float_list(value: object, default: list[float]) -> list[float]:
    """Parse a settings value into a non-empty list of floats (CSV or list)."""
    raw = _parse_str_list(value, [str(x) for x in default])
    try:
        parsed = [float(x) for x in raw]
        return parsed or list(default)
    except (TypeError, ValueError):
        logger.warning(
            "Trader: malformed numeric setting %r — falling back to default %s",
            value, default,
        )
        return list(default)


def _parse_positive_int(value: object, default: int) -> int:
    """Parse a settings value into a positive int; malformed/<=0 → default."""
    try:
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"unsupported value: {value!r}")
        parsed = int(float(value))
    except (TypeError, ValueError):
        logger.warning(
            "Trader: malformed int setting %r — falling back to default %s",
            value, default,
        )
        return default
    if parsed < 1:
        logger.warning(
            "Trader: non-positive int setting %r — falling back to default %s",
            value, default,
        )
        return default
    return parsed


def _trader_grid_settings(settings: dict) -> tuple[list[str], list[float], list[float], int]:
    """Resolve the four trader sweep parameters from a public-settings dict.

    Defaults are EXACTLY the pre-catalog hardcoded values, so an untouched
    installation sweeps the identical grid as before (zero behavior change).
    """
    freqs = _parse_str_list(settings.get("alphacrafter_rebalance_freqs"), DEFAULT_REBALANCE_FREQS)
    sizes = _parse_float_list(settings.get("alphacrafter_position_sizes"), DEFAULT_POSITION_SIZES)
    comms = _parse_float_list(settings.get("alphacrafter_commissions"), DEFAULT_COMMISSIONS)
    max_positions = _parse_positive_int(settings.get("alphacrafter_max_positions"), DEFAULT_MAX_POSITIONS)
    return freqs, sizes, comms, max_positions


class TraderAgent:
    """Agent that runs backtest sweeps and writes results to SharedMemoryH."""

    async def run(self, h: SharedMemoryH, db: Session) -> SharedMemoryH:
        """Run the Trader agent: sweep configs and backtest each.

        Reads selected_factor_ids from h.screener_outputs.
        Writes trader_outputs and agent_history to H.
        """
        settings = get_public_settings(db)
        rebalance_freqs, position_sizes, commissions, max_positions = _trader_grid_settings(settings)

        selected_factor_ids = h.screener_outputs.get("selected_factor_ids", [])
        if not selected_factor_ids:
            h.trader_outputs = {"n_configs_run": 0, "n_factors": 0}
            h.append_history("trader", "run", {"n_configs_run": 0, "n_factors": 0})
            return h

        ids = selected_factor_ids if isinstance(selected_factor_ids, list) else [x for x in selected_factor_ids.split(",") if x]
        if not ids:
            h.trader_outputs = {"n_configs_run": 0, "n_factors": 0}
            h.append_history("trader", "run", {"n_configs_run": 0, "n_factors": 0})
            return h

        factors = list(
            db.execute(select(FactorsLibrary).where(FactorsLibrary.id.in_(ids))).scalars().all()
        )
        if not factors:
            h.trader_outputs = {"n_configs_run": 0, "n_factors": 0}
            h.append_history("trader", "run", {"n_configs_run": 0, "n_factors": 0})
            return h

        universe = h.market_state.universe
        start_str, end_str = h.market_state.date_range
        start_date = datetime.fromisoformat(start_str)
        end_date = datetime.fromisoformat(end_str)

        panel = build_panel(db, universe, start_date, end_date)
        if not panel or "close" not in panel or panel["close"].empty:
            h.trader_outputs = {"n_configs_run": 0, "n_factors": len(factors)}
            h.append_history("trader", "run", {"n_configs_run": 0, "n_factors": len(factors)})
            return h

        close_df = panel["close"]
        composite = composite_score(panel, factors)
        if composite.dropna(how="all").empty:
            h.trader_outputs = {"n_configs_run": 0, "n_factors": len(factors)}
            h.append_history("trader", "run", {"n_configs_run": 0, "n_factors": len(factors)})
            return h

        configs = _generate_config_grid(rebalance_freqs, position_sizes, commissions, max_positions=max_positions)

        results: list[TraderResult] = []
        for config in configs:
            try:
                weights = build_weights(composite, config)
                equity = strategy_equity(close_df, weights, config.commission, BacktestSpec.initial_cash)
                if equity.empty or equity.isna().all():
                    continue
                prices_df = equity.rename("close").to_frame()

                spec = BacktestSpec(
                    symbols=universe,
                    start_date=start_date,
                    end_date=end_date,
                    weights=None,
                    rebalance_freq="1D",
                    commission=config.commission,
                )
                bt = await run_backtest(spec, prices_df)
                if bt.error:
                    logger.debug("Trader: backtest error for %s: %s", config, bt.error)
                    continue

                n_rebalances = int((weights.diff().abs().sum(axis=1) > 1e-9).sum())

                trader_run = TraderBacktest(
                    screener_run_id=h.screener_outputs.get("screener_run_id") or None,
                    spec_json=json.dumps(asdict(config)),
                    result_json=json.dumps(
                        {
                            "sharpe_ratio": bt.sharpe_ratio,
                            "max_drawdown": bt.max_drawdown,
                            "total_return": bt.total_return,
                            "annual_return": bt.annual_return,
                            "final_equity": bt.final_equity,
                            "rebalances": n_rebalances,
                            "n_factors": len(factors),
                        }
                    ),
                )
                db.add(trader_run)

                results.append(
                    TraderResult(
                        config=config,
                        sharpe_ratio=bt.sharpe_ratio,
                        max_drawdown=bt.max_drawdown,
                        total_return=bt.total_return,
                        trades=n_rebalances,
                    )
                )
            except Exception as exc:
                logger.debug("Trader: config %s failed: %s", config, exc)
                continue

        db.commit()

        results.sort(key=lambda r: r.sharpe_ratio, reverse=True)
        for i, r in enumerate(results):
            r.rank = i + 1

        h.trader_outputs = {
            "n_configs_run": len(results),
            "n_factors": len(factors),
            "configs": [
                {
                    "rebalance_freq": r.config.rebalance_freq,
                    "position_size": r.config.position_size,
                    "commission": r.config.commission,
                    "sharpe_ratio": r.sharpe_ratio,
                    "max_drawdown": r.max_drawdown,
                    "total_return": r.total_return,
                    "rank": r.rank,
                }
                for r in results
            ],
        }
        h.append_history(
            "trader",
            "run",
            {"n_configs_run": len(results), "n_factors": len(factors)},
        )

        return h


@dataclass
class TraderConfig:
    """A hyperparameter configuration for the Trader."""

    rebalance_freq: str = "D"  # D / W / M
    position_size: float = 0.1  # target weight per held name
    commission: float = 0.001
    max_positions: int = 5


@dataclass
class TraderResult:
    """Result of a trader backtest configuration."""

    config: TraderConfig
    sharpe_ratio: float
    max_drawdown: float
    total_return: float
    trades: int
    rank: int = 0


# --- Pure strategy helpers (synchronous, unit-testable) -----------------------


def _zscore_cross_sectional(df: pd.DataFrame) -> pd.DataFrame:
    """Per-date (row-wise) cross-sectional z-score across symbols."""
    mu = df.mean(axis=1)
    sd = cast(pd.Series, df.std(axis=1)).replace(0.0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def composite_score(
    panel: dict[str, pd.DataFrame],
    factors: list[FactorsLibrary],
) -> pd.DataFrame:
    """IC-weighted composite of the selected factors' z-scores.

    Each factor's cross-sectional z-score is multiplied by its stored mean IC,
    so factors are both oriented (a negative-IC factor flips sign) and weighted
    by strength before averaging. Higher composite ⇒ higher predicted return.
    """
    grid_index = panel["close"].index
    grid_cols = panel["close"].columns
    frames: list[np.ndarray] = []
    for f in factors:
        try:
            meta = json.loads(f.formula_json) if f.formula_json else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        dsl = meta.get("dsl")
        name = meta.get("name", f.name)
        try:
            ic = float(json.loads(f.ic_summary_json).get("ic", 0.0)) if f.ic_summary_json else 0.0
        except (json.JSONDecodeError, TypeError, ValueError):
            ic = 0.0
        try:
            vals = _factor_values(panel, name=(name if not dsl else None), dsl=dsl)
        except Exception as exc:  # invalid factor -> skip
            logger.debug("Trader: factor %s failed to evaluate: %s", f.name, exc)
            continue
        vals = vals.reindex(index=grid_index, columns=grid_cols)
        z = _zscore_cross_sectional(vals)
        frames.append((z * ic).values)

    if not frames:
        return pd.DataFrame(index=grid_index, columns=grid_cols, dtype=float)

    # Symbols that are all-NaN across the window produce an empty nanmean slice;
    # that yields NaN (dropped downstream) but warns — suppress the noise.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        stacked = np.nanmean(np.stack(frames), axis=0)
    return pd.DataFrame(stacked, index=grid_index, columns=grid_cols)


def _rebalance_dates(index: pd.Index, freq: str) -> list:
    """Trading days on which to rebalance, given a D/W/M cadence.

    Picks the last available trading day in each calendar period so we only
    rebalance on dates that actually have price data.
    """
    if freq == "D" or len(index) == 0:
        return list(index)
    rule = {"W": "W", "M": "ME"}.get(freq, "ME")
    s = pd.Series(index, index=pd.DatetimeIndex(index))
    return list(s.resample(rule).last().dropna())


def build_weights(
    composite: pd.DataFrame,
    config: TraderConfig,
) -> pd.DataFrame:
    """Long-only target-weight matrix from the composite score.

    At each rebalance date, go long the top ``max_positions`` names (by
    composite), sizing each at ``position_size`` capped so gross ≤ 100%. The
    full target vector (explicit zeros for dropped names) is set on rebalance
    dates and forward-filled until the next one.
    """
    weights = pd.DataFrame(np.nan, index=composite.index, columns=composite.columns)
    for d in _rebalance_dates(composite.index, config.rebalance_freq):
        row = composite.loc[d].dropna()
        target = pd.Series(0.0, index=composite.columns)
        if not row.empty:
            top = row.sort_values(ascending=False).head(config.max_positions)
            if len(top) > 0:
                per_name = min(config.position_size, 1.0 / len(top))
                target[top.index] = per_name
        weights.loc[d] = target.values
    return weights.ffill().fillna(0.0)


def strategy_equity(
    close_df: pd.DataFrame,
    weights: pd.DataFrame,
    commission: float,
    initial_cash: float,
) -> pd.Series:
    """Strategy equity curve from real asset returns net of turnover costs.

    Uses prior-day weights against each day's simple return and charges
    ``turnover × commission`` whenever the book is rebalanced.
    """
    rets = close_df.pct_change().fillna(0.0)
    port_ret = (weights.shift(1) * rets).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1).fillna(0.0)
    net = port_ret - turnover * commission
    return (1.0 + net).cumprod() * initial_cash


# --- Orchestration ------------------------------------------------------------


def _load_selected_factors(db: Session, screener_run_id) -> list[FactorsLibrary]:
    """Load the FactorsLibrary rows the given Screener run selected."""
    run = db.get(ScreenerRun, str(screener_run_id))
    if run is None or not run.selected_factor_ids:
        return []
    ids = [x for x in run.selected_factor_ids.split(",") if x]
    if not ids:
        return []
    return list(
        db.execute(select(FactorsLibrary).where(FactorsLibrary.id.in_(ids))).scalars().all()
    )


def _generate_config_grid(
    rebalance_freqs: list[str],
    position_sizes: list[float],
    commissions: list[float],
    *,
    max_positions: int = DEFAULT_MAX_POSITIONS,
) -> list[TraderConfig]:
    """Generate a grid of hyperparameter configurations."""
    return [
        TraderConfig(
            rebalance_freq=freq,
            position_size=size,
            commission=comm,
            max_positions=max_positions,
        )
        for freq in rebalance_freqs
        for size in position_sizes
        for comm in commissions
    ]


async def run_trader(
    db: Session,
    screener_run_id,
    universe: list[str],
    start_date: datetime,
    end_date: datetime,
    num_configs: int = 10,
    *,
    registry=None,
    max_sweep_configs: int | None = None,
) -> list[TraderResult]:
    """Sweep configs, backtest each via vectorbt, rank by Sharpe.

    Args:
        db: Database session.
        screener_run_id: Screener run whose selected factors drive the strategy.
        universe: Symbols to trade.
        start_date / end_date: Backtest window.
        num_configs: Number of top configs to return.
        registry: Provider registry override (for tests).
        max_sweep_configs: Hard cap on swept configurations. The settings-driven
            grid can grow arbitrarily; callers (orchestrator Stage 3) pass a cap
            so a fat-fingered setting cannot explode backtest runtime. When the
            grid exceeds the cap it is truncated with a WARNING — never silently.

    Returns:
        Top ``num_configs`` :class:`TraderResult` ranked by Sharpe (empty if no
        selected factors or no price data).
    """
    factors = _load_selected_factors(db, screener_run_id)
    if not factors:
        logger.warning("Trader: screener run %s has no selected factors", screener_run_id)
        return []

    panel = build_panel(db, universe, start_date, end_date, registry=registry)
    if not panel or "close" not in panel or panel["close"].empty:
        logger.warning("Trader: empty price panel for universe %s", universe)
        return []

    close_df = panel["close"]
    composite = composite_score(panel, factors)
    if composite.dropna(how="all").empty:
        logger.warning("Trader: composite score is empty (no computable factors)")
        return []

    # Settings-driven grid (audit-fixes-2026-08 todo 18): the sweep parameters
    # come from the alphacrafter_* public settings (CSV strings parsed at
    # consumption). Under untouched defaults this resolves to exactly the old
    # hardcoded triple — 2 freqs × 3 sizes × 1 commission = 6 configs.
    freqs, sizes, comms, max_positions = _trader_grid_settings(get_public_settings(db))
    configs = _generate_config_grid(freqs, sizes, comms, max_positions=max_positions)
    if max_sweep_configs is not None and len(configs) > max_sweep_configs:
        logger.warning(
            "Trader: settings-driven grid yields %d configs — truncating to cap %d "
            "(raise the cap deliberately if you need a wider sweep)",
            len(configs), max_sweep_configs,
        )
        configs = configs[:max_sweep_configs]

    results: list[TraderResult] = []
    for config in configs:
        try:
            weights = build_weights(composite, config)
            equity = strategy_equity(close_df, weights, config.commission, BacktestSpec.initial_cash)
            if equity.empty or equity.isna().all():
                continue
            prices_df = equity.rename("close").to_frame()

            spec = BacktestSpec(
                symbols=universe,
                start_date=start_date,
                end_date=end_date,
                weights=None,
                rebalance_freq="1D",
                commission=config.commission,
            )
            bt = await run_backtest(spec, prices_df)
            if bt.error:
                logger.debug("Trader: backtest error for %s: %s", config, bt.error)
                continue

            # Walk-forward pre-selection gate (Track C1): splits this config's
            # own strategy-equity curve into expanding train/test windows and
            # rejects it when the out-of-sample leg's Sharpe falls below the
            # Pardo WFE >= 0.5 hard gate (same threshold/definition as
            # alphacrafter.tuning's factor-level gate) relative to in-sample.
            # Fails open (no rejection) when there are fewer than 2 folds —
            # walk_forward_test's own Sharpe computation needs >= 2 windows
            # to have a non-degenerate std (a single fold always yields
            # exactly 0.0 for both legs, which is a cold-start, not a
            # genuinely bad WFE).
            wf_result = await walk_forward_test(spec, prices_df)
            wfe: float | None = None
            if wf_result.periods_tested >= 2:
                wfe = (
                    wf_result.out_sample_sharpe / wf_result.in_sample_sharpe
                    if wf_result.in_sample_sharpe > 1e-9
                    else 0.0
                )
                if wfe < MIN_WFE:
                    logger.debug(
                        "Trader: config %s rejected by walk-forward gate (wfe=%.3f < %.2f)",
                        config, wfe, MIN_WFE,
                    )
                    continue

            n_rebalances = int((weights.diff().abs().sum(axis=1) > 1e-9).sum())

            trader_run = TraderBacktest(
                screener_run_id=str(screener_run_id),
                spec_json=json.dumps(asdict(config)),
                result_json=json.dumps(
                    {
                        "sharpe_ratio": bt.sharpe_ratio,
                        "max_drawdown": bt.max_drawdown,
                        "total_return": bt.total_return,
                        "annual_return": bt.annual_return,
                        "final_equity": bt.final_equity,
                        "rebalances": n_rebalances,
                        "n_factors": len(factors),
                        "walk_forward_periods_tested": wf_result.periods_tested,
                        "walk_forward_wfe": wfe,
                    }
                ),
            )
            db.add(trader_run)

            results.append(
                TraderResult(
                    config=config,
                    sharpe_ratio=bt.sharpe_ratio,
                    max_drawdown=bt.max_drawdown,
                    total_return=bt.total_return,
                    trades=n_rebalances,
                )
            )
        except Exception as exc:  # one bad config shouldn't sink the sweep
            logger.debug("Trader: config %s failed: %s", config, exc)
            continue

    db.commit()

    results.sort(key=lambda r: r.sharpe_ratio, reverse=True)
    for i, r in enumerate(results):
        r.rank = i + 1
    return results[:num_configs]
