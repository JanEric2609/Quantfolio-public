"""Run the pre-registered satellite on each model's out-of-sample scores.

Step 2 (``pooled_model``) writes every stock's out-of-sample score. Here each
score picks the satellite with the rules in ``picks.py``, once per broker
(the broker sets the number of stocks and the order fee), and the picks are
traded through ``quant_lab``'s engine three times:

- gross: no costs, no tax;
- net: the broker's order fee and the spread on every trade;
- after tax: also German tax on every realized gain, losses carried forward.

Prices are total-return indices: JKP's excess return plus the US T-bill rate,
because tax falls on the whole gain. The core it competes with is the
cap-weighted universe (step 2's "market") less an ETF's TER; the core is
taxed only when sold, so its tax is an annual equivalent over
``CORE_DEFERRAL_YEARS`` with the fund's partial exemption. JKP returns are in
US dollars; currency moves hit the satellite and a world core alike and are
left out.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.foundation.data_engineering.panel_sql import query_panel
from app.foundation.data_engineering.paths import get_panel_dir
from app.foundation.factor_evidence import TILT_EVIDENCE_REGION
from app.foundation.models.entities._core import now_utc
from app.foundation.quant_metrics import newey_west_t_stat, record_trial
from app.lab.factor_premia.strategies import REGIONS
from app.lab.pooled_model import scores_path
from app.lab.quant_lab import CostModel, LabBacktestSpec, run_lab_backtest
from app.lab.satellite.picks import Review, holding_months, pick
from app.lab.satellite.rf import load_risk_free
from app.lab.satellite.spec import (
    BASELINE,
    BROKERS,
    CORE_DEFERRAL_YEARS,
    CORE_TER,
    ELIGIBLE_SIZE_GROUPS,
    FUND_PARTIAL_EXEMPTION,
    HOLD_WHILE_TOP,
    HORIZON_YEARS,
    MODELS,
    NW_LAGS,
    RECENT_YEARS,
    REVIEW_MONTHS,
    SATELLITE_EUR,
    SPREAD_BPS,
    STOCK_TAX_RATE,
    Broker,
)

logger = logging.getLogger(__name__)

TRIAL_CONTEXT = "satellite"
SCORES = (*MODELS, BASELINE)
RUNS = ("gross", "net", "after_tax")


def _costs(run: str, broker: Broker) -> CostModel:
    if run == "gross":
        return CostModel(commission_bps=0.0, spread_bps=0.0, apply_tax_drag=False)
    return CostModel(commission_bps=broker.commission_bps, spread_bps=SPREAD_BPS, apply_tax_drag=run == "after_tax")


@dataclass
class SatelliteCard:
    """The simulated record of one score's satellite, at one broker, against the core."""

    broker: str
    model: str
    region: str
    months: int
    first_month: str | None
    last_month: str | None
    trades_per_year: float
    holding_months: float | None
    gross_excess_annual: float | None
    net_excess_annual: float | None
    net_excess_t: float | None
    tracking_error: float | None
    satellite_tax_annual: float | None
    core_tax_annual: float | None
    after_tax_excess_annual: float | None
    after_tax_excess_t: float | None
    vs_baseline_annual: float | None
    vs_baseline_t: float | None
    p_beat_core: float | None
    recent: dict[str, float | int | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SatelliteStudy:
    region: str
    cards: list[SatelliteCard]
    series: pd.DataFrame
    trades: dict[str, list[dict[str, Any]]]
    artifact: Path | None = None


# --- data -----------------------------------------------------------------------


def _market(path: Path) -> pd.Series:
    """Cap-weighted excess return of the whole universe, by row month."""
    frame = query_panel(
        "SELECT eom, sum(me * r) / sum(me) AS market FROM read_parquet(?) GROUP BY eom ORDER BY eom",
        [str(path)],
    )
    return pd.Series(frame["market"].to_numpy(), index=pd.DatetimeIndex(frame["eom"]), name="market")


def _eligible_sql(select: str) -> str:
    return (
        f"SELECT {select} FROM read_parquet(?) "  # noqa: S608 - column names are module constants
        "WHERE size_grp IN (SELECT unnest(?)) AND month(eom) IN (SELECT unnest(?))"
    )


def _thresholds(path: Path) -> pd.DataFrame:
    """The hold bar per review month and score."""
    bars = ", ".join(f"quantile_cont({s}, {1.0 - HOLD_WHILE_TOP}) AS {s}" for s in SCORES)
    frame = query_panel(
        f"SELECT eom, {bars} FROM ({_eligible_sql('*')}) GROUP BY eom ORDER BY eom",  # noqa: S608
        [str(path), list(ELIGIBLE_SIZE_GROUPS), list(REVIEW_MONTHS)],
    )
    return frame.set_index(pd.DatetimeIndex(frame["eom"])).drop(columns="eom")


def _candidate_rows(path: Path, positions: int) -> pd.DataFrame:
    """Every month of every stock some score could buy.

    A buy is always among the ``2 * positions`` best-scored eligible stocks
    (at most ``positions`` are held), so those, over all months, cover both
    the pick rules and the held stocks' returns.
    """
    ranks = ", ".join(
        f"row_number() OVER (PARTITION BY eom ORDER BY {s} DESC NULLS LAST, gvkey) AS rank_{s}" for s in SCORES
    )
    best = " OR ".join(f"rank_{s} <= ?" for s in SCORES)
    frame = query_panel(
        f"""
        WITH ranked AS (SELECT gvkey, {ranks} FROM ({_eligible_sql('*')})),
        picked AS (SELECT DISTINCT gvkey FROM ranked WHERE {best})
        SELECT eom, gvkey, size_grp, r, {", ".join(SCORES)}
        FROM read_parquet(?) WHERE gvkey IN (SELECT gvkey FROM picked)
        ORDER BY eom, gvkey
        """,  # noqa: S608
        [str(path), list(ELIGIBLE_SIZE_GROUPS), list(REVIEW_MONTHS), *[2 * positions] * len(SCORES), str(path)],
    )
    frame["eom"] = pd.to_datetime(frame["eom"])
    return frame


def _rf_by_row_month(rf: pd.Series, months: pd.DatetimeIndex) -> np.ndarray:
    """RF earned over the month after each row month; the last known rate fills gaps."""
    after = pd.PeriodIndex(months, freq="M") + 1
    return rf.reindex(rf.index.union(after)).ffill().reindex(after).fillna(0.0).to_numpy()


# --- simulation -----------------------------------------------------------------


def _prices(rows: pd.DataFrame, symbols: list[str], months: pd.DatetimeIndex, rf: np.ndarray) -> pd.DataFrame:
    """Total-return price per symbol on the engine's dates (row months plus one).

    A stock with no row in a month (it left the data) keeps its last price,
    so it is sold at that price at the next review.
    """
    held = rows[rows["gvkey"].isin(symbols)]
    returns = held.pivot_table(index="eom", columns="gvkey", values="r", aggfunc="first")
    returns = returns.reindex(index=months, columns=symbols)
    total = returns.add(pd.Series(rf, index=months), axis=0).fillna(0.0)
    growth = np.vstack([np.ones(len(symbols)), np.cumprod(1.0 + total.to_numpy(), axis=0)])
    dates = months.append(pd.DatetimeIndex([months[-1] + pd.offsets.MonthEnd(1)]))
    return pd.DataFrame(growth, index=dates, columns=pd.Index(symbols))


def _weights(reviews: list[Review], prices: pd.DataFrame) -> pd.DataFrame:
    weights = pd.DataFrame(np.nan, index=prices.index, columns=prices.columns)
    for review in reviews:
        for g in review.sells:
            weights.loc[review.eom, g] = 0.0
        for g in review.buys:
            weights.loc[review.eom, g] = 1.0
    return weights


def _simulate(
    reviews: list[Review], rows: pd.DataFrame, months: pd.DatetimeIndex, rf: np.ndarray, broker: Broker,
) -> dict[str, Any]:
    """Monthly returns of each run, from the first buy on, plus the trade count."""
    symbols = sorted({g for r in reviews for g in r.buys})
    prices = _prices(rows, symbols, months, rf)
    weights = _weights(reviews, prices)
    start = int(months.get_indexer([reviews[0].eom])[0])
    out: dict[str, Any] = {}
    for name in RUNS:
        result = run_lab_backtest(
            LabBacktestSpec(
                symbols=symbols, initial_cash=SATELLITE_EUR, cost_model=_costs(name, broker),
                allowance_total_eur=Decimal("0"), rebalance="trades", offset_losses=True,
            ),
            prices, weights,
        )
        equity = np.asarray(result.equity_curve, dtype=float)
        out[name] = equity[start + 1:] / equity[start:-1] - 1.0
        out["trades"] = result.trades
    return out


# --- grading --------------------------------------------------------------------


def _annual(values: np.ndarray) -> float | None:
    return float(values.mean() * 12) if len(values) else None


def _t(values: np.ndarray) -> float | None:
    return float(newey_west_t_stat(values, NW_LAGS)) if len(values) > NW_LAGS + 1 else None


def core_tax_annual(core_annual: float) -> float:
    """The core's tax as a yearly drag: paid once, after ``CORE_DEFERRAL_YEARS``."""
    rate = STOCK_TAX_RATE * (1.0 - FUND_PARTIAL_EXEMPTION)
    growth = (1.0 + core_annual) ** CORE_DEFERRAL_YEARS
    after = (growth - rate * max(growth - 1.0, 0.0)) ** (1.0 / CORE_DEFERRAL_YEARS) - 1.0
    return core_annual - after


def _p_beat(excess_annual: float | None, tracking_error: float | None) -> float | None:
    if excess_annual is None or not tracking_error:
        return None
    z = excess_annual * math.sqrt(HORIZON_YEARS) / tracking_error
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def grade(
    broker: str, model: str, region: str, frame: pd.DataFrame, trades: int, spans: list[int], baseline: pd.DataFrame | None,
) -> SatelliteCard:
    """Grade one score's monthly series (``month``, ``gross``, ``net``, ``after_tax``, ``core``)."""
    core = frame["core"].to_numpy()
    gross, net, taxed = (frame[c].to_numpy() - core for c in ("gross", "net", "after_tax"))
    years = len(frame) / 12.0
    core_annual = float((np.prod(1.0 + core) ** (12.0 / len(core)) - 1.0)) if len(core) else 0.0
    core_tax = core_tax_annual(core_annual) if len(core) else None
    # The core's tax is paid at the end; as a yearly drag it lifts the excess.
    taxed = taxed + (core_tax or 0.0) / 12.0
    after_tax = _annual(taxed)
    te = float(net.std(ddof=1) * math.sqrt(12)) if len(net) > 1 else None
    diff = np.array([])
    if baseline is not None and model != BASELINE:
        joined = frame.merge(baseline[["month", "after_tax"]], on="month", suffixes=("", "_base"))
        diff = (joined["after_tax"] - joined["after_tax_base"]).to_numpy()
    recent = frame.tail(RECENT_YEARS * 12)
    recent_net = recent["net"].to_numpy() - recent["core"].to_numpy()
    satellite_tax = _annual(frame["net"].to_numpy() - frame["after_tax"].to_numpy())
    return SatelliteCard(
        broker=broker, model=model, region=region, months=len(frame),
        first_month=str(frame["month"].iloc[0]) if len(frame) else None,
        last_month=str(frame["month"].iloc[-1]) if len(frame) else None,
        trades_per_year=trades / years if years else 0.0,
        holding_months=float(np.mean(spans)) if spans else None,
        gross_excess_annual=_annual(gross), net_excess_annual=_annual(net), net_excess_t=_t(net),
        tracking_error=te, satellite_tax_annual=satellite_tax, core_tax_annual=core_tax,
        after_tax_excess_annual=after_tax, after_tax_excess_t=_t(taxed),
        vs_baseline_annual=_annual(diff), vs_baseline_t=_t(diff),
        p_beat_core=_p_beat(after_tax, te),
        recent={"months": len(recent), "net_excess_annual": _annual(recent_net), "net_excess_t": _t(recent_net)},
    )


# --- the study ------------------------------------------------------------------


def results_path(panel_dir: Path, region: str) -> Path:
    """Where ``run_satellite_study`` writes the cards, trades and monthly series."""
    return panel_dir / "derived" / f"satellite_{region}_results.json"


def _write_artifact(panel_dir: Path, study: SatelliteStudy) -> Path:
    out = results_path(panel_dir, study.region)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "computed_at": now_utc().isoformat(), "region": study.region,
        "spec": {
            "satellite_eur": SATELLITE_EUR,
            "brokers": [
                {"name": b.name, "positions": b.positions, "order_fee_eur": b.order_fee_eur,
                 "commission_bps": b.commission_bps}
                for b in BROKERS
            ],
            "spread_bps": SPREAD_BPS, "review_months": list(REVIEW_MONTHS), "hold_while_top": HOLD_WHILE_TOP,
            "eligible_size_groups": list(ELIGIBLE_SIZE_GROUPS), "core_ter": CORE_TER,
            "stock_tax_rate": STOCK_TAX_RATE, "core_deferral_years": CORE_DEFERRAL_YEARS,
        },
        "cards": [c.as_dict() for c in study.cards],
        "trades": study.trades,
        "series": study.series.assign(month=study.series["month"].astype(str)).to_dict(orient="records"),
    }, indent=1, default=float))
    return out


def run_satellite_study(
    db: Session,
    *,
    region: str = TILT_EVIDENCE_REGION,
    panel_dir: Path | None = None,
    persist: bool = True,
) -> SatelliteStudy | None:
    """Simulate the pre-registered satellite for each score; None without step 2's scores.

    Every score runs at every broker in ``BROKERS``. With ``persist`` each
    model is recorded once per broker on the trial ledger (context
    ``satellite``) and the cards, trades and monthly series are written to
    ``derived/satellite_<region>_results.json`` for step 4.
    """
    if region not in REGIONS:
        raise ValueError(f"unknown region {region!r}; expected one of {sorted(REGIONS)}")
    panel_dir = panel_dir or get_panel_dir()
    path = scores_path(panel_dir, region)
    if not path.exists():
        logger.warning("satellite: no out-of-sample scores at %s; run app.lab.pooled_model first", path)
        return None

    market = _market(path)
    months = cast(pd.DatetimeIndex, market.index)
    rf = _rf_by_row_month(load_risk_free(panel_dir / "derived"), months)
    core = market.to_numpy() + rf - CORE_TER / 12.0
    rows = _candidate_rows(path, max(b.positions for b in BROKERS))
    thresholds = _thresholds(path)
    reviews_at = [m for m in months if m.month in REVIEW_MONTHS]

    series: dict[tuple[str, str], pd.DataFrame] = {}
    trades: dict[str, list[dict[str, Any]]] = {}
    counts: dict[tuple[str, str], tuple[int, list[int]]] = {}
    for broker in BROKERS:
        for score in SCORES:
            reviews = pick(rows, score, cast(pd.Series, thresholds[score]), reviews_at, broker.positions)
            if not reviews:
                continue
            runs = _simulate(reviews, rows, months, rf, broker)
            start = int(months.get_indexer([reviews[0].eom])[0])
            series[(broker.name, score)] = pd.DataFrame({
                "broker": broker.name,
                "model": score,
                "month": (pd.PeriodIndex(months, freq="M") + 1)[start:],
                **{name: runs[name] for name in RUNS},
                "core": core[start:],
            })
            trades[f"{broker.name}:{score}"] = [
                {"eom": str(r.eom.date()), "sells": list(r.sells), "buys": list(r.buys)} for r in reviews
            ]
            counts[(broker.name, score)] = (runs["trades"], holding_months(reviews, list(months)))
            logger.info("satellite: %s at %s simulated (%d reviews with trades)", score, broker.name, len(reviews))

    if not series:
        return None
    cards = [
        grade(b, score, region, series[(b, score)], *counts[(b, score)], series.get((b, BASELINE)))
        for b, score in series
    ]
    frame = pd.concat(series.values(), ignore_index=True)
    study = SatelliteStudy(region, cards, frame, trades)
    if persist:
        for broker in BROKERS:
            for name in MODELS:
                record_trial(
                    db, TRIAL_CONTEXT, f"{region}:{broker.name}:{name}",
                    {"region": region, "broker": broker.name, "model": name, "pre_registered": True},
                )
        db.commit()
        study.artifact = _write_artifact(panel_dir, study)
        logger.info("satellite: wrote %s", study.artifact)
    return study
