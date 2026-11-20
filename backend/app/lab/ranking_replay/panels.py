"""Historical ingredient panels for the locked 2019–2025 exam (ADR 0019 amendment 1).

Turns monthly rows of the point-in-time JKP characteristics panel into one
cross-section per month-end and ingredient (``period``, ``entity``,
``score``, ``forward``), the input :func:`replay.run_replay` takes. Each
score uses data up to and including its month-end only; the live formulas
are those of ``decision/discover/composite.py`` and ``pipeline.py``, with the
daily inputs rebuilt from monthly data as the amendment lists.

Pure pandas: :func:`build_panels` takes the rows, :func:`load_jkp_rows`
reads them from the Parquet panel with DuckDB.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

#: The 12 eurozone markets of Discover's DAX 40 / EuroStoxx 50 seed.
EUROZONE: tuple[str, ...] = (
    "DEU", "FRA", "NLD", "ITA", "ESP", "BEL", "FIN", "IRL", "AUT", "PRT", "LUX", "GRC",
)
#: Developed Europe (live benchmark: a developed-Europe ETF).
EUROPE: tuple[str, ...] = (*EUROZONE, "GBR", "CHE", "DNK", "SWE", "NOR")
#: Developed world (live portfolio reference: the MSCI World core fund).
WORLD: tuple[str, ...] = (*EUROPE, "USA", "CAN", "JPN", "AUS")
BENCHMARK_SIZE_GROUPS: tuple[str, ...] = ("mega", "large")

#: Universe per region and month: the largest stocks by market equity.
TOP_N_PER_REGION = 100

#: Live WEIGHTS (composite.py) of the five non-AI ingredients.
COMPOSITE_WEIGHTS: dict[str, float] = {
    "portfolio": 0.15,
    "fundamentals": 0.15,
    "momentum": 0.15,
    "risk": 0.10,
    "benchmark": 0.05,
}

# Live constants (composite.py / pipeline.py).
_VOL_MAX = 0.60
_CVAR_MAX = 0.06
_MDD_MAX = 0.30
#: Normal expected shortfall at 95 %: phi(1.645) / 0.05.
_CVAR95_PER_SIGMA = math.exp(-0.5 * 1.6448536269514722**2) / math.sqrt(2 * math.pi) / 0.05

RISK_MONTHS = 12
BENCHMARK_MONTHS = 36
FIT_MONTHS = 24
MIN_MONTHS = 12

COLUMNS: tuple[str, ...] = (
    "gvkey", "permno", "eom", "excntry", "size_grp", "me", "mom_12_1",
    "be_me", "gp_at", "at_gr1", "ret_1_0", "rvol_21d", "ret_exc_lead1m",
)


def _entity(gvkey: Any, permno: Any) -> str:
    perm = "" if pd.isna(permno) else str(int(permno))
    return f"{gvkey}:{perm}"


def prepare_rows(raw: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate on (gvkey, permno, eom) keeping the largest ``me``; add ``entity``."""
    df = raw.loc[:, list(COLUMNS)].copy()
    df["eom"] = pd.to_datetime(df["eom"]).dt.normalize()
    df["entity"] = [_entity(g, p) for g, p in zip(df["gvkey"], df["permno"])]
    df = df.sort_values("me", ascending=False, na_position="last")
    df = df.drop_duplicates(subset=["entity", "eom"], keep="first")
    return df.sort_values(["eom", "entity"]).reset_index(drop=True)


def benchmark_returns(df: pd.DataFrame, countries: tuple[str, ...]) -> tuple[pd.Series, pd.Series]:
    """Value-weighted monthly return and next-month excess of *countries*' large caps.

    Members are the ``mega``/``large`` stocks at the month-end the weights
    come from. The monthly return (``ret_1_0`` of month *t*) weights by the
    previous calendar month-end's ``me`` and keeps a member's return even if
    it left the size groups by *t*; the forward leg (``ret_exc_lead1m`` at
    *t*) weights by this month-end's ``me``. A month with no rows leaves the
    next month without weights instead of reusing older ones.
    """
    rows = cast(pd.DataFrame, df[df["excntry"].isin(countries)])
    ever = rows.loc[rows["size_grp"].isin(BENCHMARK_SIZE_GROUPS), "entity"].unique()
    rows = cast(pd.DataFrame, rows[rows["entity"].isin(ever)])
    rows = rows.assign(member=rows["size_grp"].isin(BENCHMARK_SIZE_GROUPS).astype(float))
    months = pd.date_range(rows["eom"].min(), rows["eom"].max(), freq="ME")

    def _wide(column: str) -> pd.DataFrame:
        return rows.pivot(index="eom", columns="entity", values=column).reindex(months)

    weights = _wide("me").where(_wide("member").eq(1.0))

    def _weighted(values: pd.DataFrame, w: pd.DataFrame) -> pd.Series:
        w = w.where(values.notna())
        total = w.sum(axis=1, min_count=1)
        return (values * w).sum(axis=1, min_count=1) / total

    # The index is every calendar month-end, so shift(1) is exactly one month.
    monthly = _weighted(_wide("ret_1_0"), weights.shift(1))
    forward = _weighted(_wide("ret_exc_lead1m"), weights)
    return monthly, forward


def universe(df: pd.DataFrame, eom: pd.Timestamp, top_n: int = TOP_N_PER_REGION) -> pd.DataFrame:
    """The month's largest *top_n* US and *top_n* eurozone stocks by ``me``."""
    month = cast(pd.DataFrame, df[(df["eom"] == eom) & df["me"].notna()])
    us = cast(pd.DataFrame, month[month["excntry"] == "USA"]).nlargest(top_n, "me")
    ez = cast(pd.DataFrame, month[month["excntry"].isin(EUROZONE)]).nlargest(top_n, "me")
    return pd.concat([us.assign(region="us"), ez.assign(region="europe")])


def momentum_scores(mom: pd.Series) -> pd.Series:
    """Live ``assign_momentum_ranks``: mid-rank percentile ``(rank − 0.5) / n``."""
    present = mom.dropna()
    ranks = present.rank(method="average")
    return (ranks - 0.5) / len(present)


def max_drawdown(returns: np.ndarray) -> float:
    """Largest peak-to-trough fall of the compounded path (starting at 1)."""
    wealth = np.concatenate(([1.0], np.cumprod(1.0 + returns)))
    peaks = np.maximum.accumulate(wealth)
    return float(np.max(1.0 - wealth / peaks))


def risk_score(rvol_daily: np.ndarray, returns: np.ndarray) -> float | None:
    """Live risk score from 12 months of daily-vol readings and monthly returns."""
    rvol = rvol_daily[~np.isnan(rvol_daily)]
    rets = returns[~np.isnan(returns)]
    if len(rvol) < MIN_MONTHS // 2 or len(rets) < MIN_MONTHS // 2:
        return None
    sigma_daily = float(np.sqrt(np.mean(rvol**2)))
    vol_pen = min(1.0, sigma_daily * math.sqrt(252) / _VOL_MAX)
    cvar_pen = min(1.0, sigma_daily * _CVAR95_PER_SIGMA / _CVAR_MAX)
    mdd_pen = min(1.0, max_drawdown(rets) / _MDD_MAX)
    return max(0.0, min(1.0, 1.0 - (0.4 * vol_pen + 0.3 * cvar_pen + 0.3 * mdd_pen)))


def _annualised(returns: np.ndarray) -> float:
    """Geometric annual rate; a path that lost everything is −100 %."""
    wealth = float(np.prod(np.maximum(1.0 + returns, 0.0)))
    if wealth <= 0.0:
        return -1.0
    return wealth ** (12.0 / len(returns)) - 1.0


def benchmark_score(stock: np.ndarray, bench: np.ndarray) -> float | None:
    """Live ``0.5 + 0.5·tanh(t/2)`` with t = IR·√years on monthly active returns."""
    ok = ~(np.isnan(stock) | np.isnan(bench))
    s, b = stock[ok], bench[ok]
    if len(s) < MIN_MONTHS:
        return None
    excess = _annualised(s) - _annualised(b)
    tracking_error = float(np.std(s - b, ddof=1)) * math.sqrt(12)
    if tracking_error <= 0:
        return 0.5
    t_stat = excess / tracking_error * math.sqrt(len(s) / 12.0)
    return 0.5 + 0.5 * math.tanh(t_stat / 2.0)


def portfolio_score(stock: np.ndarray, world: np.ndarray) -> float:
    """Live stock fit ``1 − 0.6·(corr+1)/2`` against the core (world) fund."""
    ok = ~(np.isnan(stock) | np.isnan(world))
    if ok.sum() < MIN_MONTHS:
        return 0.5
    corr = float(np.corrcoef(stock[ok], world[ok])[0, 1])
    if math.isnan(corr):
        corr = 0.0
    return max(0.0, min(1.0, 1.0 - 0.6 * max(0.0, min(1.0, (corr + 1.0) / 2.0))))


def fundamentals_score(be_me: Any, gp_at: Any, at_gr1: Any) -> float | None:
    """Live ``factor_characteristics_score`` (pipeline.stage_quant_signals)."""
    if pd.isna(be_me) and pd.isna(gp_at) and pd.isna(at_gr1):
        return None
    score = 0.5
    if pd.notna(be_me):
        score += 0.15 * max(-1.0, min(1.0, (float(be_me) - 0.5) / 0.5))
    if pd.notna(gp_at):
        score += 0.15 * max(-1.0, min(1.0, (float(gp_at) - 0.3) / 0.3))
    if pd.notna(at_gr1):
        score -= 0.15 * max(-1.0, min(1.0, float(at_gr1) / 0.2))
    return round(max(0.0, min(1.0, score)), 4)


def composite_score(signals: dict[str, float | None]) -> float | None:
    """Live weighted composite, renormalised over the ingredients present."""
    present = {k: v for k, v in signals.items() if v is not None and k in COMPOSITE_WEIGHTS}
    total = sum(COMPOSITE_WEIGHTS[k] for k in present)
    if total <= 0:
        return None
    return sum(COMPOSITE_WEIGHTS[k] * v for k, v in present.items()) / total


def _window(series: pd.Series | None, eom: pd.Timestamp, months: int) -> np.ndarray:
    """The last *months* month-ends up to and including *eom*, NaN where missing."""
    index = pd.date_range(end=eom, periods=months, freq="ME")
    if series is None:
        return np.full(months, np.nan)
    return series.reindex(index).to_numpy(dtype=float)


def build_panels(
    raw: pd.DataFrame, periods: list[pd.Timestamp], *, top_n: int = TOP_N_PER_REGION,
) -> dict[str, pd.DataFrame]:
    """One (period, entity, score, forward) frame per ingredient plus ``composite``."""
    df = prepare_rows(raw)
    region_bench = {"us": benchmark_returns(df, ("USA",)), "europe": benchmark_returns(df, EUROPE)}
    world_monthly, _ = benchmark_returns(df, WORLD)

    by_entity = {entity: rows.set_index("eom") for entity, rows in df.groupby("entity", sort=False)}
    rows_out: dict[str, list[dict[str, Any]]] = {k: [] for k in (*COMPOSITE_WEIGHTS, "composite")}

    for eom in periods:
        eom = cast(pd.Timestamp, pd.Timestamp(eom)).normalize()
        members = universe(df, eom, top_n)
        if members.empty:
            continue
        momentum = momentum_scores(cast(pd.Series, members.set_index("entity")["mom_12_1"]))
        for row in cast(list[dict[str, Any]], members.to_dict("records")):
            monthly_bench, forward_bench = region_bench[row["region"]]
            lead = row["ret_exc_lead1m"]
            bench_lead = forward_bench.get(eom)
            if pd.isna(lead) or bench_lead is None or pd.isna(bench_lead):
                continue
            forward = float(lead) - float(bench_lead)
            hist = by_entity.get(row["entity"])
            rets = cast(pd.Series, hist["ret_1_0"]) if hist is not None else None
            rvol = cast(pd.Series, hist["rvol_21d"]) if hist is not None else None
            signals: dict[str, float | None] = {
                "momentum": float(momentum[row["entity"]]) if row["entity"] in momentum.index else None,
                "risk": risk_score(_window(rvol, eom, RISK_MONTHS), _window(rets, eom, RISK_MONTHS)),
                "benchmark": benchmark_score(
                    _window(rets, eom, BENCHMARK_MONTHS), _window(monthly_bench, eom, BENCHMARK_MONTHS),
                ),
                "portfolio": portfolio_score(_window(rets, eom, FIT_MONTHS), _window(world_monthly, eom, FIT_MONTHS)),
                "fundamentals": fundamentals_score(row["be_me"], row["gp_at"], row["at_gr1"]),
            }
            signals["composite"] = composite_score(signals)
            for name, score in signals.items():
                if score is not None:
                    rows_out[name].append({
                        "period": eom.strftime("%Y-%m-%d"), "entity": row["entity"],
                        "score": float(score), "forward": forward,
                    })
    return {name: pd.DataFrame(rows, columns=pd.Index(["period", "entity", "score", "forward"]))
            for name, rows in rows_out.items()}


def load_jkp_rows(panel_dir: Path, start: str, end: str) -> pd.DataFrame:
    """The JKP rows of the developed-world markets between *start* and *end*."""
    import duckdb

    source = (panel_dir / "wrds_factor_characteristics_pit" / "*.parquet").as_posix()
    con = duckdb.connect()
    try:
        con.execute("SET memory_limit='3GB'")
        con.execute("SET threads=2")
        return con.execute(
            "SELECT gvkey, permno, eom, excntry, size_grp, me, mom_12_1, be_me, gp_at, at_gr1, "
            "ret_1_0, rvol_21d, ret_exc_lead1m FROM read_parquet(?, union_by_name=true) "
            "WHERE eom BETWEEN ? AND ? AND list_contains(?, excntry)",
            [source, start, end, list(WORLD)],
        ).df()
    finally:
        con.close()


def main() -> None:
    """CLI: ``python -m app.lab.ranking_replay.panels --out DIR`` writes one Parquet per ingredient."""
    import argparse

    from app.foundation.data_engineering import get_panel_dir
    from app.lab.ranking_replay.replay import EXAM_END_YEAR, EXAM_START_YEAR

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--panel-dir", type=Path, default=None)
    args = parser.parse_args()

    # Three years of history before the first exam month feed the benchmark window.
    raw = load_jkp_rows(
        args.panel_dir or get_panel_dir(), f"{EXAM_START_YEAR - 3}-01-01", f"{EXAM_END_YEAR}-12-31",
    )
    periods = list(pd.date_range(f"{EXAM_START_YEAR}-01-31", f"{EXAM_END_YEAR}-12-31", freq="ME"))
    panels = build_panels(raw, periods)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, frame in panels.items():
        frame.to_parquet(args.out / f"{name}.parquet", index=False)
        print(f"{name}: {len(frame)} rows over {frame['period'].nunique()} periods")


if __name__ == "__main__":
    main()
