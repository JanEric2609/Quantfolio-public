"""Factor zoo: Fama-French data download, factor exposure, and per-asset attribution.

Downloads daily Developed Europe 5-factor + Europe momentum data from Kenneth
French's data library via httpx, caches locally. Europe (not US) factors are
used throughout because the portfolio this app manages is EUR/DE-scoped
(currency=EUR, tax_residency_country=DE, DKB/German-broker-only) — see
docs/adr/0007-fama-french-europe-factors.md. There is deliberately no
US-factor fallback: if the Europe download fails, callers fall back to the
existing ETF-proxy approach instead of silently mixing regions.
"""
from __future__ import annotations

import io
import os
import zipfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

_FF5_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Europe_5_Factors_Daily_CSV.zip"
_MOM_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Europe_Mom_Factor_Daily_CSV.zip"
# US 5-factor file, for single US listings only (ADR 0007 amendment,
# 2026-09-28). A US share's daily return regressed on Developed-Europe
# factors mixes markets that close 5.5 hours apart: SPY itself loaded 0.50
# on Europe's market factor (R^2 0.54) against 0.99 on the US one (R^2 0.99).
_FF5_US_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
# Japan for Tokyo listings; Developed for any other non-US, non-European one.
_FF5_JAPAN_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Japan_5_Factors_Daily_CSV.zip"
_FF5_DEVELOPED_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Developed_5_Factors_Daily_CSV.zip"

# Configurable via QUANT_FACTORS_FF_CACHE_DIR env var or Control Center setting.
_DEFAULT_CACHE_DIR = "/tmp/quantfolio_ff_cache"


def _get_cache_dir() -> Path:
    """Resolve cache dir: env var first, then DB setting, then default."""
    env_val = os.environ.get("QUANT_FACTORS_FF_CACHE_DIR")
    if env_val:
        return Path(env_val)
    try:
        from app.foundation.settings import get_public_settings
        from app.foundation.core.db import SessionLocal

        with SessionLocal() as db:
            settings = get_public_settings(db)
            db_val = settings.get("quant_factors_ff_cache_dir")
            if db_val:
                return Path(db_val)
    except Exception:
        pass
    return Path(_DEFAULT_CACHE_DIR)


_CACHE_DIR = _get_cache_dir()

FACTOR_ZOO = {
    "ff3": {
        "name": "Fama-French Developed Europe 5 Factors",
        "factors": ["mkt_rf", "smb", "hml", "rmw", "cma"],
        "description": (
            "Market excess return, Small-minus-Big, High-minus-Low, "
            "Robust-minus-Weak profitability, Conservative-minus-Aggressive "
            "investment (Developed Europe, daily)"
        ),
        "source": "Kenneth R. French Data Library (Developed Europe)",
        "url": _FF5_URL,
    },
    "momentum": {
        "name": "Europe Momentum Factor (WML)",
        "factors": ["mom"],
        "description": "Prior 2-12 month momentum factor (Developed Europe, daily)",
        "source": "Kenneth R. French Data Library (Developed Europe)",
        "url": _MOM_URL,
    },
    "etf_proxy": {
        "name": "ETF Proxy Factors",
        "factors": ["market", "size", "value", "momentum"],
        "description": "ETF-proxy factors: EUNL.DE, IUSN.DE, IWVL.L, IS3R.DE",
        "source": "local_price_cache",
        "url": None,
    },
}


def list_factor_zoo() -> list[dict[str, Any]]:
    return [{"id": k, **v} for k, v in FACTOR_ZOO.items()]


def _download_ff_csv(url: str, cache_path: Path) -> Any:
    try:
        import httpx
        import pandas as pd

        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if cache_path.exists():
            age_days = (date.today() - date.fromtimestamp(cache_path.stat().st_mtime)).days
            if age_days < 7:
                return pd.read_parquet(cache_path)

        resp = httpx.get(url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            csv_name = next(n for n in zf.namelist() if n.endswith(".CSV") or n.endswith(".csv"))
            raw = zf.read(csv_name).decode("utf-8", errors="replace")

        lines = raw.splitlines()
        # Find the start of daily data block (skip header text until we hit the data)
        start = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("19") or line.strip().startswith("20"):
                start = i
                break

        data_lines = []
        for line in lines[start:]:
            stripped = line.strip()
            if not stripped:
                break  # first blank line ends the daily block
            data_lines.append(stripped)

        text = "\n".join(data_lines)
        df = pd.read_csv(io.StringIO(text), header=None)
        df.columns = ["date"] + [f"col{i}" for i in range(1, len(df.columns))]
        df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d", errors="coerce")
        df = df.dropna(subset=["date"]).set_index("date")
        df = df.apply(pd.to_numeric, errors="coerce")
        # Dartmouth marks missing observations as -99.99; no real daily factor
        # return is anywhere near -99%, so mask before scaling to avoid a
        # -0.9999 poison value entering the regression.
        df = df.mask(df <= -99.0)
        df = df / 100.0  # convert percentage to decimal

        df.to_parquet(cache_path)
        return df
    except Exception:
        return None


def _name_ff5_columns(df: Any) -> Any:
    # Columns: Mkt-RF, SMB, HML, RMW, CMA, RF
    cols = list(df.columns)
    rename = {}
    if len(cols) >= 5:
        rename[cols[0]] = "mkt_rf"
        rename[cols[1]] = "smb"
        rename[cols[2]] = "hml"
        rename[cols[3]] = "rmw"
        rename[cols[4]] = "cma"
        if len(cols) >= 6:
            rename[cols[5]] = "rf"
    elif len(cols) >= 3:
        rename[cols[0]] = "mkt_rf"
        rename[cols[1]] = "smb"
        rename[cols[2]] = "hml"
        if len(cols) >= 4:
            rename[cols[3]] = "rf"
    return df.rename(columns=rename)


def _load_ff3() -> Any:
    """Load the Developed Europe 5-factor set (mkt_rf, smb, hml, rmw, cma).

    Named ``_load_ff3`` for historical/API-compatibility reasons (callers use
    ``get_ff3_returns``), but the underlying source is the Europe 5-factor
    file — see docs/adr/0007-fama-french-europe-factors.md.
    """
    cache = _CACHE_DIR / "ff5_europe.parquet"
    df = _download_ff_csv(_FF5_URL, cache)
    if df is None or df.empty:
        return None
    return _name_ff5_columns(df)


def _load_ff5_us() -> Any:
    """Load the US 5-factor set, same columns as :func:`_load_ff3`."""
    cache = _CACHE_DIR / "ff5_us.parquet"
    df = _download_ff_csv(_FF5_US_URL, cache)
    if df is None or df.empty:
        return None
    return _name_ff5_columns(df)


def _load_ff5_file(url: str, cache_name: str) -> Any:
    """Load one 5-factor file, same columns as :func:`_load_ff3`."""
    df = _download_ff_csv(url, _CACHE_DIR / cache_name)
    if df is None or df.empty:
        return None
    return _name_ff5_columns(df)


def _load_momentum() -> Any:
    cache = _CACHE_DIR / "mom_europe.parquet"
    df = _download_ff_csv(_MOM_URL, cache)
    if df is None or df.empty:
        return None
    cols = list(df.columns)
    return df.rename(columns={cols[0]: "mom"})


def get_ff3_returns(
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """Developed Europe 5-factor returns: the portfolio-level factor set."""
    return _factor_returns(_load_ff3(), start, end)


def get_ff5_returns_for_listing(
    region: str,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """The factor set of a single security's own market.

    ``region`` is ``providers.utils.listing_region``: ``"us"`` gets the US
    research factors, ``"europe"`` Developed Europe, ``"japan"`` Japan and
    anything else the Developed-markets set. A per-stock regression must use
    factors that trade in the same session as the stock; the portfolio-level
    attribution keeps :func:`get_ff3_returns` (ADR 0007).
    """
    if region == "us":
        df = _load_ff5_us()
    elif region == "europe":
        df = _load_ff3()
    elif region == "japan":
        df = _load_ff5_file(_FF5_JAPAN_URL, "ff5_japan.parquet")
    else:
        region = "developed"
        df = _load_ff5_file(_FF5_DEVELOPED_URL, "ff5_developed.parquet")
    result = _factor_returns(df, start, end)
    result["region"] = region
    return result


def _factor_returns(df: Any, start: date | None, end: date | None) -> dict[str, Any]:
    if df is None:
        return {"status": "unavailable", "message": "Could not download Fama-French data.", "factors": {}}
    if start:
        df = df[df.index >= str(start)]
    if end:
        df = df[df.index <= str(end)]
    factors = {col: df[col].dropna().to_dict() for col in df.columns if col != "rf"}
    return {"status": "completed", "factors": {k: {str(d.date()): float(v) for d, v in vals.items()} for k, vals in factors.items()}}


def get_momentum_returns(
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    df = _load_momentum()
    if df is None:
        return {"status": "unavailable", "message": "Could not download momentum factor data.", "factors": {}}
    if start:
        df = df[df.index >= str(start)]
    if end:
        df = df[df.index <= str(end)]
    factors = {col: df[col].dropna().to_dict() for col in df.columns}
    return {"status": "completed", "factors": {k: {str(d.date()): float(v) for d, v in vals.items()} for k, vals in factors.items()}}


def compute_factor_attribution(
    portfolio_returns: list[float],
    factor_returns: dict[str, list[float]],
    factor_names: list[str] | None = None,
) -> dict[str, Any]:
    """OLS attribution of portfolio returns on a factor set.

    The lists are paired by position from the tail, so they must already be
    aligned date-for-date. Callers holding dated returns should use
    :func:`compute_factor_attribution_by_date` instead: Ken French publishes
    with a lag of a month or more, so a tail-aligned daily stock series ending
    today and a factor series ending weeks earlier regress each day's return
    on a different day's factors.
    """
    try:
        import numpy as np

        if factor_names is None:
            factor_names = list(factor_returns.keys())

        n = min(len(portfolio_returns), min(len(v) for v in factor_returns.values()))
        if n < max(20, len(factor_names) + 5):
            return {"status": "unavailable", "message": "Not enough overlapping observations."}

        y = np.array(portfolio_returns[-n:], dtype=float)
        X_data = np.column_stack([np.array(factor_returns[f][-n:], dtype=float) for f in factor_names])
        X_design = np.column_stack([np.ones(n), X_data])
        coeffs, *_ = np.linalg.lstsq(X_design, y, rcond=None)
        fitted = X_design @ coeffs
        residual = y - fitted
        ss_res = float((residual**2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum())
        r2 = 0.0 if ss_tot == 0 else float(1 - ss_res / ss_tot)

        exposures = {f: float(c) for f, c in zip(factor_names, coeffs[1:])}
        return {
            "status": "completed",
            "alpha_daily": float(coeffs[0]),
            "r_squared": r2,
            "exposures": exposures,
        }
    except Exception as exc:
        return {"status": "failed", "message": str(exc)}


def align_returns_to_factors(
    returns: Sequence[float],
    dates: Sequence[Any],
    factors_by_date: dict[str, dict[str, float]],
    factor_names: list[str] | None = None,
) -> tuple[list[float], dict[str, list[float]], list[str]]:
    """Inner-join a dated return series with date-keyed factor series.

    ``dates[i]`` is the date of ``returns[i]``; anything ``str()`` renders as
    ``YYYY-MM-DD...`` works (``date``, ``Timestamp``, ISO string). Only dates
    present in the return series and in every requested factor survive, in
    ascending order. Returns ``(returns, factor_lists, dates)``.
    """
    if len(returns) != len(dates):
        raise ValueError(f"returns ({len(returns)}) and dates ({len(dates)}) differ in length")
    names = factor_names or list(factors_by_date.keys())
    by_date = {str(d)[:10]: float(r) for d, r in zip(dates, returns)}
    common = set(by_date)
    for name in names:
        common &= set(factors_by_date.get(name, {}))
    ordered = sorted(common)
    aligned_factors = {name: [float(factors_by_date[name][d]) for d in ordered] for name in names}
    return [by_date[d] for d in ordered], aligned_factors, ordered


def restate_in_usd(
    returns: Sequence[float],
    dates: Sequence[Any],
    usd_per_unit: dict[str, float],
    first_start: Any | None = None,
) -> tuple[list[float], list[str]]:
    """Restate local-currency daily returns in USD.

    Ken French's international factors are USD returns, and the Fama-French
    papers regress USD stock returns on them. A EUR return regressed on them
    carries the EURUSD move as noise: on 2019-2026 daily data SAP.DE loaded
    0.77 on the market and -0.42 on SMB in EUR against 1.01 and -0.11 in USD,
    and the MSCI World ETF EUNL.DE showed a spurious -0.35 SMB tilt.

    ``usd_per_unit`` maps ``YYYY-MM-DD`` to USD per unit of the local currency.
    ``returns[i]`` is the return from ``dates[i-1]`` to ``dates[i]``; the
    first one starts at ``first_start`` (the first price date), and is dropped
    when that is not given. A missing rate carries the last one forward.
    Returns ``(usd_returns, dates)`` for the kept rows.
    """
    if len(returns) != len(dates):
        raise ValueError(f"returns ({len(returns)}) and dates ({len(dates)}) differ in length")
    rate_dates = sorted(usd_per_unit)
    out_returns: list[float] = []
    out_dates: list[str] = []
    j = 0
    last_rate: float | None = None
    if first_start is not None:
        start_key = str(first_start)[:10]
        while j < len(rate_dates) and rate_dates[j] <= start_key:
            value = usd_per_unit[rate_dates[j]]
            if value and value > 0:
                last_rate = float(value)
            j += 1
    prev_rate: float | None = last_rate
    for r, d in zip(returns, dates):
        key = str(d)[:10]
        while j < len(rate_dates) and rate_dates[j] <= key:
            value = usd_per_unit[rate_dates[j]]
            if value and value > 0:
                last_rate = float(value)
            j += 1
        if last_rate is not None and prev_rate is not None:
            out_returns.append((1.0 + float(r)) * (last_rate / prev_rate) - 1.0)
            out_dates.append(key)
        prev_rate = last_rate
    return out_returns, out_dates


def compute_factor_attribution_by_date(
    returns: Sequence[float],
    dates: Sequence[Any],
    factors_by_date: dict[str, dict[str, float]],
    factor_names: list[str] | None = None,
) -> dict[str, Any]:
    """:func:`compute_factor_attribution` on a date-joined sample.

    Adds ``n_observations`` and ``aligned_dates`` so callers can compute
    contributions over exactly the window the regression used.
    """
    names = factor_names or list(factors_by_date.keys())
    aligned_returns, aligned_factors, aligned_dates = align_returns_to_factors(
        returns, dates, factors_by_date, names
    )
    result = compute_factor_attribution(aligned_returns, aligned_factors, names)
    result["n_observations"] = len(aligned_dates)
    result["aligned_dates"] = aligned_dates
    return result


def get_factor_definitions() -> list[dict]:
    """Return the seed pool of factor definitions for the AlphaCrafter Miner.

    Returns definitions that work with pure OHLCV data (``momentum_12_1``,
    ``volatility_21d``, ``rsi_14``, ``sma_ratio_50_200``, ``volume_21d_mean``)
    plus fundamental factors (``value_ep``, ``size_log_mc``, ``quality_roe``)
    whose values are NaN when the underlying data is unavailable — the cross-
    sectional IC computation naturally handles missing values.
    """
    return [
        {
            "name": "momentum_12_1",
            "description": "12-1 month price momentum",
            "category": "momentum",
            "formula": "(close.shift(21) / close.shift(252)) - 1",
        },
        {
            "name": "volatility_21d",
            "description": "21-day realised volatility (annualised)",
            "category": "risk",
            "formula": "close.pct_change().rolling(21).std() * sqrt(252)",
        },
        {
            "name": "rsi_14",
            "description": "14-day Relative Strength Index",
            "category": "momentum",
            "formula": "rsi(close, 14)",
        },
        {
            "name": "sma_ratio_50_200",
            "description": "50-day vs 200-day SMA ratio (trend strength)",
            "category": "trend",
            "formula": "close.rolling(50).mean() / close.rolling(200).mean() - 1",
        },
        {
            "name": "volume_21d_mean",
            "description": "21-day average volume (liquidity proxy)",
            "category": "liquidity",
            "formula": "volume.rolling(21).mean()",
        },
        {
            "name": "value_ep",
            "description": "Earnings/Price ratio (inverse P/E)",
            "category": "value",
            "formula": "1 / pe_ratio",
        },
        {
            "name": "size_log_mc",
            "description": "Log market cap (size factor — negative exposure is small-cap tilt)",
            "category": "size",
            "formula": "log(market_cap)",
        },
        {
            "name": "quality_roe",
            "description": "Return on equity",
            "category": "quality",
            "formula": "roe",
        },
    ]


def compute_factor(name: str, prices: "Any") -> "Any":
    """Compute a factor series from an OHLCV prices DataFrame.

    Args:
        name:   Factor name (must match an entry in get_factor_definitions()).
        prices: DataFrame with at minimum a "close" column (and optionally
                "pe_ratio", "market_cap", "roe" for fundamental factors).

    Returns:
        pd.Series with the factor values aligned to the prices index.

    Raises:
        ValueError: If the factor name is unknown.
    """
    if name == "momentum_12_1":
        # 12-month minus 1-month momentum: return relative to 252 days ago,
        # skipping the most recent month (21 trading days) to avoid reversal.
        return (prices["close"].shift(21) / prices["close"].shift(252)) - 1

    if name == "volatility_21d":
        # Annualised 21-day realised volatility.
        return prices["close"].pct_change().rolling(21).std() * (252**0.5)

    if name == "rsi_14":
        # 14-day Relative Strength Index.
        delta = prices["close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("nan"))
        return (100 - 100 / (1 + rs)).rename("rsi_14")

    if name == "sma_ratio_50_200":
        # 50-day vs 200-day SMA ratio (trend strength).
        sma_50 = prices["close"].rolling(50).mean()
        sma_200 = prices["close"].rolling(200).mean()
        return (sma_50 / sma_200 - 1).rename("sma_ratio_50_200")

    if name == "volume_21d_mean":
        # 21-day average volume (liquidity proxy).
        if "volume" not in prices.columns:
            return pd.Series(float("nan"), index=prices.index, name="volume_21d_mean")
        return prices["volume"].rolling(21).mean().rename("volume_21d_mean")

    if name == "value_ep":
        # Earnings/Price = 1 / PE. When the fundamentals column is missing,
        # emit an all-NaN series so the cross-sectional IC naturally produces
        # ~0 (factor marked invalid) rather than raising and relying on the
        # caller's exception handler.
        if "pe_ratio" not in prices.columns:
            return pd.Series(float("nan"), index=prices.index, name="value_ep")
        pe = prices["pe_ratio"].replace(0, float("nan"))
        return (1.0 / pe).rename("value_ep")

    if name == "size_log_mc":
        # Log market cap — emit all-NaN when column is missing.
        if "market_cap" not in prices.columns:
            return pd.Series(float("nan"), index=prices.index, name="size_log_mc")

        import numpy as np

        mc = prices["market_cap"].replace(0, float("nan"))
        return np.log(mc).rename("size_log_mc")

    if name == "quality_roe":
        # Return on equity — emit all-NaN when column is missing.
        if "roe" not in prices.columns:
            return pd.Series(float("nan"), index=prices.index, name="quality_roe")
        return prices["roe"].rename("quality_roe")

    raise ValueError(f"Unknown factor: {name}")


def persist_factor_scores(
    db: Any,
    run_id: str,
    symbol: str,
    scores: dict[str, float],
    as_of: date | None = None,
) -> None:
    """Persist factor scores to QuantFactorScore rows."""
    from app.foundation.models.entities import QuantFactorScore

    today = as_of or date.today()
    for factor_name, value in scores.items():
        row = QuantFactorScore(
            run_id=run_id,
            symbol=symbol,
            factor_name=factor_name,
            factor_value=value,
            factor_score=None,
            as_of_date=today,
        )
        db.add(row)
    db.commit()


def newey_west_se(
    residuals: list[float],
    X: "Any",
    lags: int | None = None,
) -> dict[str, Any]:
    """Compute Newey-West (HAC) standard errors for OLS regression.

    Implements the Newey-West (1987) heteroskedasticity and autocorrelation
    consistent covariance matrix estimator. This corrects the naive OLS
    standard errors when residuals exhibit autocorrelation or heteroskedasticity.

    Args:
        residuals: OLS residuals (length n).
        X: Design matrix (n × k), including intercept column.
            Can be numpy array, list of lists, or pandas DataFrame.
        lags: Number of lags for the Newey-West estimator.
            If None, uses the default bandwidth: floor(4 * (n/100)^(2/9)).

    Returns:
        dict with:
            - "hac_cov": Newey-West covariance matrix (k × k)
            - "hac_se": Standard errors for each coefficient (length k)
            - "lags": Number of lags used
            - "n": Number of observations

    References:
        Newey, W. K., & West, K. D. (1987). A Simple, Positive Semi-Definite,
        Heteroskedasticity and Autocorrelation Consistent Covariance Matrix.
        Econometrica, 55(3), 703-708.
    """
    import math

    import numpy as np

    n = len(residuals)
    if n < 2:
        return {"hac_cov": None, "hac_se": None, "lags": 0, "n": n}

    e = np.asarray(residuals, dtype=float).reshape(-1, 1)
    X_arr = np.asarray(X, dtype=float)

    if X_arr.ndim == 1:
        X_arr = X_arr.reshape(-1, 1)

    k = X_arr.shape[1]

    # Default bandwidth: floor(4 * (n/100)^(2/9))
    if lags is None:
        lags = int(math.floor(4 * (n / 100) ** (2 / 9)))
    lags = min(lags, n - 1)

    # White's sandwich: X'X inverse
    XtX = X_arr.T @ X_arr
    try:
        XtX_inv = np.linalg.inv(XtX)
    except np.linalg.LinAlgError:
        XtX_inv = np.linalg.pinv(XtX)

    # Omega_0: heteroskedasticity-consistent (White) term
    # Sum of e_i^2 * x_i * x_i'
    S0 = np.zeros((k, k))
    for i in range(n):
        xi = X_arr[i, :].reshape(k, 1)
        S0 += (e[i, 0] ** 2) * (xi @ xi.T)

    # Newey-West autocorrelation terms
    S_nw = S0.copy()
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1)  # Bartlett kernel weight
        S_lag = np.zeros((k, k))
        for i in range(lag, n):
            xi = X_arr[i, :].reshape(k, 1)
            xi_lag = X_arr[i - lag, :].reshape(k, 1)
            S_lag += e[i, 0] * e[i - lag, 0] * (xi @ xi_lag.T)
        S_nw += weight * (S_lag + S_lag.T)

    # HAC covariance matrix
    hac_cov = XtX_inv @ S_nw @ XtX_inv

    # Standard errors: sqrt of diagonal
    hac_se = np.sqrt(np.maximum(np.diag(hac_cov), 0.0))

    return {
        "hac_cov": hac_cov.tolist(),
        "hac_se": hac_se.tolist(),
        "lags": lags,
        "n": n,
    }
