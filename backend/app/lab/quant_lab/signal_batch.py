"""First signal batch (ADR 0015 ruling #26: "classic factors + price/volume
microstructure + ML on the panel").

Scope note on "classic factors": the PIT panel (``data_engineering.panel_schema``)
carries OHLCV only, no fundamentals (P/E, market cap, book value) -- so
literal Fama-French-style value/size factors (which need cross-sectional
fundamentals regressions) aren't computable from it yet. This batch instead
computes the price/volume factors that ARE computable and are still
legitimate, widely-used cross-sectional equity factors: 12-1 momentum,
realized volatility, and dollar-volume liquidity. True fundamentals-based
classic factors are deferred until the panel carries fundamentals data (a
natural extension once a real Datastream PIT extract lands, per Phase 3).

IC/ICIR cross-sectional evaluation mirrors ``app.lab.alphacrafter.
miner``'s proven approach (rank correlation via ``corrwith``, std-floored
information ratio) -- reimplemented standalone here since ``alphacrafter``
is a decision-loop package and this package is foundation-tier (same
constraint as ``cv.py``).

Persists to Parquet (not a new DB table), consistent with Phase 3's
Parquet-not-Postgres precedent for lab-owned data. Not wired into any live
advisor/discover/graduation pathway -- that repoint is a later ADR phase.
"""
from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd

from app.foundation.data_engineering.panel_read import read_panel
from app.lab.quant_lab.cv import purged_embargo_splits
from app.lab.quant_ml.labels import vol_scaled_forward_return

logger = logging.getLogger(__name__)

_DEFAULT_SIGNAL_BATCH_DIR = "/tmp/quantfolio_signal_batch"
DEFAULT_MIN_IC = 0.02
DEFAULT_MIN_ICIR = 0.3


def get_signal_batch_dir() -> Path:
    """Resolve the signal batch output dir: env var -> DB setting -> default."""
    env_val = os.environ.get("QUANTFOLIO_SIGNAL_BATCH_DIR")
    if env_val:
        return Path(env_val)
    try:
        from app.foundation.core.db import SessionLocal
        from app.foundation.settings import get_public_settings

        with SessionLocal() as db:
            settings = get_public_settings(db)
            db_val = settings.get("quant_lab_signal_batch_dir")
            if db_val:
                return Path(db_val)
    except Exception:
        pass
    return Path(_DEFAULT_SIGNAL_BATCH_DIR)


def get_signal_gate() -> tuple[float, float]:
    """Resolve the (min IC, min ICIR) survival gate: DB setting -> default."""
    try:
        from app.foundation.core.db import SessionLocal
        from app.foundation.settings import get_public_settings

        with SessionLocal() as db:
            settings = get_public_settings(db)
        return (
            float(settings.get("quant_lab_min_ic", DEFAULT_MIN_IC)),
            float(settings.get("quant_lab_min_icir", DEFAULT_MIN_ICIR)),
        )
    except Exception:
        logger.warning("Signal gate settings unreadable; using defaults", exc_info=True)
        return DEFAULT_MIN_IC, DEFAULT_MIN_ICIR


# Currency codes quoted in a minor unit, and the exact factor converting them
# to their major unit. These are definitional, not FX rates: LSEG/Datastream
# quote UK lines in pence, and 100 pence is one pound by definition, on every
# date. Any other minor-unit quotation (ZAc, ILA, ...) has to be normalised in
# the extract itself -- only the codes listed here are recognised.
_MINOR_UNIT_TO_MAJOR: dict[str, tuple[str, float]] = {
    "gbp": ("GBP", 0.01),
    "gbx": ("GBP", 0.01),
    "ukp": ("GBP", 0.01),
}
# Case matters for exactly one of these: "GBp" is pence, "GBP" is pounds. The
# lookup below is therefore case-sensitive on purpose, unlike everything else
# that touches currency codes in this codebase.
_MINOR_UNIT_CODES: frozenset[str] = frozenset({"GBp", "GBX", "GBx", "UKp"})


@dataclass
class PricePanel:
    close: pd.DataFrame
    volume: pd.DataFrame
    # symbol -> quoted currency code, as it appeared in the panel. Empty means
    # "no currency information", which is treated as a single-currency panel
    # (the pre-existing behaviour, and correct for a hand-built test panel).
    currencies: dict[str, str] = field(default_factory=dict)


def build_price_panel(symbols: list[str] | None = None) -> PricePanel:
    """Pivot the PIT panel (``data_engineering.panel_read.read_panel``) into
    (date x symbol) close and volume matrices, chronologically sorted.

    Also carries each symbol's quoted currency through, which the price
    matrices themselves cannot express. Without it a cross-sectional factor
    built from a price *level* -- dollar volume -- silently ranks quotation
    unit rather than liquidity across a multi-currency universe like the
    STOXX Europe 600.
    """
    raw = read_panel(symbols=symbols)
    if raw.empty:
        return PricePanel(close=pd.DataFrame(), volume=pd.DataFrame())
    close = raw.pivot_table(index="as_of_date", columns="symbol", values="close", aggfunc="last")
    volume = raw.pivot_table(index="as_of_date", columns="symbol", values="volume", aggfunc="last")
    close = cast(pd.DataFrame, close.sort_index())
    volume = cast(pd.DataFrame, volume.reindex(close.index))

    currencies: dict[str, str] = {}
    if "currency" in raw.columns:
        # Last observed code wins for a symbol that redenominated mid-history;
        # read_panel returns rows sorted by (symbol, as_of_date), so "last" is
        # chronologically last, not file-order last.
        with_ccy = raw.dropna(subset=["currency"])
        currencies = {
            str(sym): str(ccy)
            for sym, ccy in with_ccy.drop_duplicates(subset=["symbol"], keep="last")
            .set_index("symbol")["currency"]
            .items()
        }
    return PricePanel(close=close, volume=volume, currencies=currencies)


def _split_quotation_unit(code: str) -> tuple[str, float]:
    """Split a quoted currency code into (major currency, unit multiplier)."""
    if code in _MINOR_UNIT_CODES:
        return _MINOR_UNIT_TO_MAJOR[code.lower()]
    return code.upper(), 1.0


def base_currency_multipliers(
    panel: PricePanel, fx_to_base: Mapping[str, float] | None = None
) -> dict[str, float] | None:
    """Per-symbol multiplier putting every quoted price on one scale.

    Returns ``None`` when the panel spans more than one currency and
    *fx_to_base* does not cover all of them -- the caller must then skip any
    factor built from price levels rather than compute a meaningless one.
    *fx_to_base* maps a major currency code to its value in the base currency
    (e.g. ``{"EUR": 1.0, "GBP": 1.17, "CHF": 1.05}``); a single-currency panel
    needs no rates at all.
    """
    columns = [str(c) for c in panel.close.columns]
    if not panel.currencies:
        return dict.fromkeys(columns, 1.0)

    split = {sym: _split_quotation_unit(panel.currencies[sym]) for sym in columns if sym in panel.currencies}
    majors = {major for major, _ in split.values()}
    unknown = [sym for sym in columns if sym not in split]

    if len(majors) <= 1 and not unknown:
        return {sym: unit for sym, (_, unit) in split.items()}

    rates = {str(k).upper(): float(v) for k, v in (fx_to_base or {}).items()}
    missing_rates = sorted(majors - rates.keys())
    if missing_rates or unknown:
        logger.warning(
            "price panel spans %d currencies (%s); no usable common scale -- missing FX rate(s) for %s, "
            "unknown currency for %d symbol(s)",
            len(majors),
            sorted(majors),
            missing_rates or "none",
            len(unknown),
        )
        return None
    return {sym: unit * rates[major] for sym, (major, unit) in split.items()}


def compute_microstructure_factors(
    panel: PricePanel, fx_to_base: Mapping[str, float] | None = None
) -> dict[str, pd.DataFrame]:
    """12-1 momentum, 20-day realized vol, 20-day dollar volume.

    Momentum and realized volatility are ratios of prices in one series, so
    they are invariant to the currency each symbol is quoted in. Dollar volume
    is not: ``close * volume`` mixes a price level with a share count, so a
    pence-quoted line reads ~100x more liquid than an identical euro-quoted
    one. It is therefore computed on prices converted to a common base and
    **omitted entirely** when no such conversion is available -- an absent
    factor is recoverable, a factor that ranks quotation unit is not.
    """
    close = panel.close
    factors: dict[str, pd.DataFrame] = {
        "momentum_12_1": cast(pd.DataFrame, (close.shift(21) / close.shift(252)) - 1.0),
        "realized_vol_20d": cast(
            pd.DataFrame, close.pct_change(fill_method=None).rolling(window=20, min_periods=20).std()
        ),
    }

    multipliers = base_currency_multipliers(panel, fx_to_base)
    if multipliers is None:
        logger.warning("omitting dollar_volume_20d: panel is multi-currency and no FX rates were supplied")
        return factors

    scale = pd.Series(multipliers, dtype="float64").reindex(close.columns).fillna(1.0)
    close_base = cast(pd.DataFrame, close.mul(scale, axis=1))
    dollar_volume = cast(pd.DataFrame, close_base * panel.volume)
    factors["dollar_volume_20d"] = cast(pd.DataFrame, dollar_volume.rolling(window=20, min_periods=20).mean())
    return factors


def _cross_sectional_ic(factor_values: pd.DataFrame, fwd_returns: pd.DataFrame) -> pd.Series:
    """Per-date cross-sectional Spearman IC (rank correlation via corrwith).

    Mirrors ``app.lab.alphacrafter.miner.cross_sectional_ic`` -- see
    module docstring for why this is reimplemented rather than imported.
    """
    common = factor_values.index.intersection(fwd_returns.index)
    if common.empty:
        return pd.Series(dtype=float)
    fr = factor_values.loc[common].rank(axis=1)
    rr = fwd_returns.loc[common].rank(axis=1)
    enough = (factor_values.loc[common].notna() & fwd_returns.loc[common].notna()).sum(axis=1) >= 3
    ic = fr.corrwith(rr, axis=1)
    ic[~enough] = np.nan
    return ic.dropna()


def _icir(ic_series: pd.Series, std_floor: float = 1e-3) -> float:
    """IC information ratio: mean(IC) / std(IC), std floored to avoid a
    stable near-zero-dispersion series producing a spuriously huge ratio."""
    if ic_series.empty:
        return 0.0
    std = max(float(ic_series.std()), std_floor)
    return float(ic_series.mean()) / std


@dataclass
class FactorResult:
    name: str
    ic: float
    icir: float
    n_oos_observations: int
    valid: bool


@dataclass
class SignalBatchResult:
    universe: list[str]
    horizon: int
    factors: list[FactorResult] = field(default_factory=list)
    output_path: Path | None = None

    @property
    def valid_factors(self) -> list[FactorResult]:
        return [f for f in self.factors if f.valid]


def run_signal_batch(
    symbols: list[str] | None = None,
    horizon: int = 20,
    vol_window: int = 60,
    n_folds: int = 5,
    purge_days: int = 5,
    embargo_frac: float = 0.01,
    min_ic: float | None = None,
    min_icir: float | None = None,
    fx_to_base: Mapping[str, float] | None = None,
    dry_run: bool = True,
) -> SignalBatchResult:
    """Build the panel, compute factors + the vol-scaled label, evaluate each
    factor's out-of-sample IC/ICIR across purge/embargo folds, and (if not
    ``dry_run``) persist the report + survivors to Parquet.

    *fx_to_base* maps a major currency code to its value in a common base
    (e.g. ``{"EUR": 1.0, "GBP": 1.17}``). It is only needed for a
    multi-currency panel, and only by the dollar-volume factor, which is
    skipped without it -- see ``compute_microstructure_factors``.

    *min_ic* / *min_icir* default to the ``quant_lab_min_ic`` /
    ``quant_lab_min_icir`` settings (Control Center › AlphaCrafter & Quant Lab).
    """
    if min_ic is None or min_icir is None:
        setting_ic, setting_icir = get_signal_gate()
        min_ic = setting_ic if min_ic is None else min_ic
        min_icir = setting_icir if min_icir is None else min_icir
    panel = build_price_panel(symbols)
    universe = list(panel.close.columns) if not panel.close.empty else (symbols or [])
    result = SignalBatchResult(universe=universe, horizon=horizon)

    if panel.close.empty or len(panel.close) < (n_folds + 1) * max(purge_days + 1, 10):
        logger.info("run_signal_batch: insufficient panel rows for %d-fold walk-forward; nothing evaluated", n_folds)
        return result

    labels = vol_scaled_forward_return(panel.close, horizon=horizon, vol_window=vol_window)
    factors = compute_microstructure_factors(panel, fx_to_base=fx_to_base)

    folds = purged_embargo_splits(len(panel.close), n_folds=n_folds, purge_days=purge_days, embargo_frac=embargo_frac)

    for name, factor_df in factors.items():
        oos_ic_chunks: list[pd.Series] = []
        for f in folds:
            test_idx = panel.close.index[f.test_positions]
            fold_ic = _cross_sectional_ic(factor_df.loc[test_idx], labels.loc[test_idx])
            if not fold_ic.empty:
                oos_ic_chunks.append(fold_ic)
        ic_series = pd.concat(oos_ic_chunks) if oos_ic_chunks else pd.Series(dtype=float)
        ic_mean = float(ic_series.mean()) if not ic_series.empty else 0.0
        ic_ir = _icir(ic_series)
        valid = abs(ic_mean) > min_ic and abs(ic_ir) > min_icir
        result.factors.append(FactorResult(name=name, ic=ic_mean, icir=ic_ir, n_oos_observations=len(ic_series), valid=valid))

    if not dry_run:
        out_dir = get_signal_batch_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        report = pd.DataFrame([
            {"name": f.name, "ic": f.ic, "icir": f.icir, "n_oos_observations": f.n_oos_observations, "valid": f.valid}
            for f in result.factors
        ])
        out_path = out_dir / "signal_batch_report.parquet"
        report.to_parquet(out_path, index=False)
        result.output_path = out_path

    return result


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the report to Parquet (default: dry-run, print only)")
    parser.add_argument("--symbol", action="append", help="limit to specific symbol(s); default: everything in the PIT panel")
    parser.add_argument(
        "--fx",
        action="append",
        metavar="CCY=RATE",
        help="value of a currency in the base currency, e.g. --fx EUR=1.0 --fx GBP=1.17. "
        "Only needed for a multi-currency panel, and only by the dollar-volume factor",
    )
    args = parser.parse_args()

    fx_to_base: dict[str, float] | None = None
    if args.fx:
        fx_to_base = {}
        for pair in args.fx:
            code, _, rate = pair.partition("=")
            if not rate:
                parser.error(f"--fx expects CCY=RATE, got {pair!r}")
            fx_to_base[code.strip().upper()] = float(rate)

    logging.basicConfig(level=logging.INFO)
    result = run_signal_batch(symbols=args.symbol, fx_to_base=fx_to_base, dry_run=not args.apply)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    logger.info("%s: %d symbols, %d factors evaluated, %d survived the IC/ICIR gate", mode, len(result.universe), len(result.factors), len(result.valid_factors))
    for f in result.factors:
        logger.info("  %s: IC=%.4f ICIR=%.2f n=%d valid=%s", f.name, f.ic, f.icir, f.n_oos_observations, f.valid)
    if result.output_path:
        logger.info("  written to %s", result.output_path)


if __name__ == "__main__":
    _main()
