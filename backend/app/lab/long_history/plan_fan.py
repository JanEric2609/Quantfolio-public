"""The plan's fan and one-year band from 150 years of history (plan 2026-10-14, D6).

The planner (``foundation/wealth_planner.py``) draws from one parametric model:
a published real return, the book's volatility and Student-t shocks. This is
the second opinion from history, built the way ADR 0022 built its test:

* **Real returns for a saver at home** in each JST country (:func:`home_panel`),
  every equity series lowered by 30 % of its own mean real premium over bills
  (ADR 0022's primary haircut).
* **The euro-area homes** by default (Belgium, Finland, France, Germany, Italy,
  the Netherlands, Portugal, Spain); all 16 are reported next to them.
* **A stationary block bootstrap** of whole years (mean block 10), one home per
  path, homes drawn with equal weight.
* **The book's mix**: global equity, home bonds and home bills (cash), rebalanced
  each year without cost.
* **Annual steps.** Twelve monthly contributions go in at the start of each
  year, constant in real terms.

German tax is applied once, on a full sale at the horizon: 26.375 % on the
nominal gain above one year's allowance, 30 % of an equity fund's gain exempt.
The basis is kept nominal, so inflation raises the tax in real terms. The
Vorabpauschale is not modelled separately: it is credited against this tax.
Rebalancing gains are not taxed here.

The JST file is CC BY-NC-SA: it is read from a local path (:func:`jst_path`)
and never enters the repository or the public export.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.lab.long_history.jst_loader import COUNTRIES, home_panel, load_jst
from app.lab.long_history.plan_bootstrap import BILL, BOND, GLOBAL, bootstrap_indices, haircut

EURO_HOMES = ("Belgium", "Finland", "France", "Germany", "Italy", "Netherlands", "Portugal", "Spain")
PERCENTILES = (5, 25, 50, 75, 95)
BAND_PERCENTILES = (10, 50, 90)
HAIRCUT_SHARE = 0.30
#: TER a year on global equity, bonds and cash (ADR 0022's costs).
TER = (0.0020, 0.0015, 0.0010)
TAX_RATE = 0.26375
EQUITY_EXEMPT = 0.30
#: One year's saver's allowance, in today's euros (single filer).
ALLOWANCE_EUR = 1000.0
#: Years whose start decides the "bad start" stress.
BAD_START_YEARS = 10
SEED = 20261015
MODEL_VERSION = "jst-r6-bootstrap-v1"
SOURCE = (
    "Jordà-Schularick-Taylor Macrohistory Database R.6 (Jordà, Knoll, Kuvshinov, Schularick and Taylor, "
    "'The Rate of Return on Everything, 1870-2015', QJE 2019), CC BY-NC-SA 4.0"
)
#: Where the file lives unless ``QUANTFOLIO_JST_PATH`` says otherwise: <repo>/data/long_history/jst_r6/.
_DEFAULT_PATH = Path(__file__).resolve().parents[4] / "data" / "long_history" / "jst_r6" / "JSTdatasetR6.dta"


def jst_path() -> Path:
    return Path(os.environ.get("QUANTFOLIO_JST_PATH") or _DEFAULT_PATH)


@dataclass(frozen=True)
class Mix:
    """Shares of global equity, home bonds and cash; they sum to 1."""

    equity: float = 1.0
    bond: float = 0.0
    cash: float = 0.0

    def weights(self) -> np.ndarray:
        w = np.array([self.equity, self.bond, self.cash], dtype=float)
        total = w.sum()
        return w / total if total > 0 else np.array([1.0, 0.0, 0.0])

    def taxable_share(self) -> float:
        w = self.weights()
        return float(w[0] * (1.0 - EQUITY_EXEMPT) + w[1] + w[2])


@dataclass(frozen=True)
class HomeHistory:
    country: str
    years: np.ndarray
    #: Real returns a year: global equity (haircut), home bonds, home bills.
    real: np.ndarray
    inflation: np.ndarray


def histories(raw: pd.DataFrame, homes: tuple[str, ...] = EURO_HOMES,
              haircut_share: float = HAIRCUT_SHARE) -> list[HomeHistory]:
    """Each home's real returns and inflation, years where every input exists."""
    out = []
    for home in homes:
        panel = home_panel(raw, home)
        if panel.empty:
            continue
        cpi = raw[raw["country"] == home].set_index("year")["cpi"].astype(float)
        infl = (cpi / cpi.shift(1) - 1.0).reindex(panel.index)
        ok = infl.notna().to_numpy()
        arr = haircut(panel.to_numpy(dtype=float), haircut_share) if haircut_share else panel.to_numpy(dtype=float)
        arr = arr[ok][:, [GLOBAL, BOND, BILL]]
        if len(arr) < 2 * BAD_START_YEARS:
            continue
        out.append(HomeHistory(home, panel.index.to_numpy()[ok], arr, infl.to_numpy(dtype=float)[ok]))
    return out


@lru_cache(maxsize=2)
def _load(path: str, _mtime: float) -> tuple[tuple[HomeHistory, ...], tuple[HomeHistory, ...]]:
    raw = load_jst(path)
    return tuple(histories(raw, EURO_HOMES)), tuple(histories(raw, COUNTRIES))


def load_histories() -> tuple[tuple[HomeHistory, ...], tuple[HomeHistory, ...]] | None:
    """(euro-area homes, all 16) from the local JST file, or None when it is not there. Cached per file version."""
    path = jst_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return _load(str(path), mtime)


def _paths(hist: list[HomeHistory] | tuple[HomeHistory, ...], mix: Mix, years: int, paths: int,
           seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Net real portfolio returns and inflation per path and year, shape (paths, years) each."""
    rng = np.random.default_rng(seed)
    home = rng.integers(0, len(hist), paths)
    w = mix.weights()
    ter = float(np.dot(w, TER))
    ret = np.empty((paths, years))
    infl = np.empty((paths, years))
    for h, hh in enumerate(hist):
        rows = np.flatnonzero(home == h)
        if len(rows) == 0:
            continue
        idx = bootstrap_indices(np.arange(len(hh.real)), len(rows), years, rng)
        ret[rows] = hh.real[idx] @ w - ter
        infl[rows] = hh.inflation[idx]
    return ret, infl


def simulate(hist: list[HomeHistory] | tuple[HomeHistory, ...], *, start_value: float, monthly_contribution: float,
             years: int, mix: Mix = Mix(), paths: int = 10_000, seed: int = SEED) -> dict[str, np.ndarray]:
    """Real wealth by year (paths x years+1), the nominal basis in real terms at the horizon, and each path's
    real growth over the first :data:`BAD_START_YEARS` years."""
    ret, infl = _paths(hist, mix, years, paths, seed)
    c = 12.0 * monthly_contribution
    wealth = np.empty((paths, years + 1))
    wealth[:, 0] = start_value
    w = np.full(paths, float(start_value))
    basis = np.full(paths, float(start_value))
    for t in range(years):
        w = (w + c) * (1.0 + ret[:, t])
        # A nominal basis loses real value with each year's inflation.
        basis = (basis + c) / (1.0 + infl[:, t])
        wealth[:, t + 1] = w
    first = min(BAD_START_YEARS, max(1, years // 2))
    return {"wealth": wealth, "basis": basis, "start_growth": np.prod(1.0 + ret[:, :first], axis=1)}


def after_tax(wealth: np.ndarray, basis: np.ndarray, mix: Mix) -> np.ndarray:
    """Real wealth after selling everything at the horizon (German flat tax on the nominal gain)."""
    gain = np.clip(wealth - basis, 0.0, None) * mix.taxable_share()
    return wealth - TAX_RATE * np.clip(gain - ALLOWANCE_EUR, 0.0, None)


def project(hist_euro: list[HomeHistory] | tuple[HomeHistory, ...],
            hist_all: list[HomeHistory] | tuple[HomeHistory, ...], *, start_value: float,
            monthly_contribution: float, years: int, mix: Mix = Mix(), goal: float | None = None,
            paths: int = 10_000, seed: int = SEED) -> dict[str, Any]:
    """The fan, the after-tax and bad-start views, in the shape of ``wealth_planner.project``."""
    sim = simulate(hist_euro, start_value=start_value, monthly_contribution=monthly_contribution, years=years,
                   mix=mix, paths=paths, seed=seed)
    wealth = sim["wealth"]
    contributed = [start_value + 12.0 * monthly_contribution * y for y in range(years + 1)]
    fan = []
    for y in range(years + 1):
        q = np.percentile(wealth[:, y], PERCENTILES)
        fan.append({"year": y, **{f"p{pc}": float(v) for pc, v in zip(PERCENTILES, q)},
                    "contributed": contributed[y]})
    terminal = wealth[:, -1]
    taxed = after_tax(terminal, sim["basis"], mix)
    bad = sim["start_growth"] <= np.percentile(sim["start_growth"], 10)
    other = simulate(hist_all, start_value=start_value, monthly_contribution=monthly_contribution, years=years,
                     mix=mix, paths=paths, seed=seed)["wealth"][:, -1]
    first = min(BAD_START_YEARS, max(1, years // 2))
    out: dict[str, Any] = {
        "inputs": {
            "start_value_eur": start_value, "monthly_contribution_eur": monthly_contribution, "years": years,
            "mix": {"equity": float(mix.weights()[0]), "bond": float(mix.weights()[1]),
                    "cash": float(mix.weights()[2])},
            "paths": paths, "seed": seed, "goal_eur": goal,
        },
        "fan": fan,
        "terminal": {
            "median": float(np.median(terminal)), "mean": float(np.mean(terminal)),
            "p5": float(np.percentile(terminal, 5)), "p95": float(np.percentile(terminal, 95)),
            "contributed": contributed[-1],
            "p_below_contributed": float(np.mean(terminal < contributed[-1])),
        },
        "after_tax": {
            "median": float(np.median(taxed)), "p5": float(np.percentile(taxed, 5)),
            "p95": float(np.percentile(taxed, 95)),
            "median_tax": float(np.median(terminal - taxed)),
        },
        "bad_start": {
            "first_years": first,
            "median": float(np.median(terminal[bad])), "p5": float(np.percentile(terminal[bad], 5)),
        },
        "all_countries": {
            "homes": len(hist_all), "median": float(np.median(other)),
            "p5": float(np.percentile(other, 5)), "p95": float(np.percentile(other, 95)),
        },
        "assumptions": {
            "homes": [h.country for h in hist_euro],
            "years_of_history": int(sum(len(h.real) for h in hist_euro)),
            "haircut": HAIRCUT_SHARE, "mean_block_years": 10, "ter": dict(zip(("equity", "bond", "cash"), TER)),
            "tax_rate": TAX_RATE, "equity_exempt": EQUITY_EXEMPT, "allowance_eur": ALLOWANCE_EUR,
            "source": SOURCE, "model_version": MODEL_VERSION,
        },
        "real_terms": True,
        "estimate": True,
        "not_tax_advice": True,
    }
    if goal is not None and goal > 0:
        out["goal"] = {"goal_eur": goal, "probability": float(np.mean(terminal >= goal)),
                       "probability_after_tax": float(np.mean(taxed >= goal))}
    return out


def one_year_band(hist: list[HomeHistory] | tuple[HomeHistory, ...], mix: Mix = Mix(),
                  percentiles: tuple[int, ...] = BAND_PERCENTILES) -> dict[int, float]:
    """Quantiles of next year's real return of *mix*: every home-year, each home weighted equally."""
    w = mix.weights()
    ter = float(np.dot(w, TER))
    values = np.concatenate([h.real @ w - ter for h in hist])
    weights = np.concatenate([np.full(len(h.real), 1.0 / len(h.real)) for h in hist])
    order = np.argsort(values)
    cum = np.cumsum(weights[order]) / weights.sum()
    last = len(values) - 1
    return {p: float(values[order][min(int(np.searchsorted(cum, p / 100.0)), last)]) for p in percentiles}
