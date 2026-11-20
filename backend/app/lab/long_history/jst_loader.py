"""The JST Macrohistory panel, restated per home country in real terms (ADR 0022, "Construction")."""
from __future__ import annotations

from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd

#: The 16 countries with equity total returns in JST R.6.
COUNTRIES = (
    "Australia", "Belgium", "Denmark", "Finland", "France", "Germany", "Italy", "Japan",
    "Netherlands", "Norway", "Portugal", "Spain", "Sweden", "Switzerland", "UK", "USA",
)
#: Real annual returns in the home currency, in this column order.
ASSETS = ("global_eq", "home_eq", "intl_eq", "bond", "bill")
_NEEDED = ("eq_tr", "bond_tr", "bill_rate", "cpi")


def load_jst(path: str | Path) -> pd.DataFrame:
    """The raw panel: one row per country and year, the columns ADR 0022 uses."""
    df = cast(pd.DataFrame, pd.read_stata(path))
    df = df[df["country"].isin(COUNTRIES)]
    cols = ["country", "year", "eq_tr", "bond_tr", "bill_rate", "cpi", "xrusd", "gdp"]
    out = cast(pd.DataFrame, df[cols]).copy()
    out["year"] = out["year"].astype(int)
    return out.sort_values(["country", "year"]).reset_index(drop=True)


def home_panel(raw: pd.DataFrame, home: str) -> pd.DataFrame:
    """Real annual returns for a saver at home in *home*, indexed by year (columns :data:`ASSETS`).

    A year enters only with every home input and the previous year's CPI and
    exchange rate. Foreign equity is converted into the home currency through
    the dollar rates; the global and international legs are weighted by the
    previous year's GDP in dollars over the countries present that year.
    """
    wide = raw.pivot(index="year", columns="country")
    years = wide.index
    fx = cast(pd.DataFrame, wide["xrusd"])  # local units per USD
    eq = cast(pd.DataFrame, wide["eq_tr"])
    gdp_usd = (cast(pd.DataFrame, wide["gdp"]) / fx).shift(1)
    home_cpi = cast(pd.Series, cast(pd.DataFrame, wide["cpi"])[home])
    infl = home_cpi / home_cpi.shift(1) - 1.0

    # Equity of every country j in home currency: (1 + r_j) * FX_t / FX_{t-1}, FX = xrusd_h / xrusd_j.
    cross = fx[[home]].to_numpy() / fx.to_numpy()
    cross = pd.DataFrame(cross, index=years, columns=fx.columns)
    eq_home_ccy = (1.0 + eq) * (cross / cross.shift(1)) - 1.0
    eq_home_ccy[home] = eq[home]

    def weighted(cols: list[str]) -> pd.Series:
        r = eq_home_ccy[cols]
        w = gdp_usd[cols].where(r.notna())
        return (r * w).sum(axis=1, min_count=1) / w.sum(axis=1, min_count=1)

    present = [c for c in COUNTRIES if c in eq.columns]
    nominal = pd.DataFrame({
        "global_eq": weighted(present),
        "home_eq": eq[home],
        "intl_eq": weighted([c for c in present if c != home]),
        "bond": wide["bond_tr"][home],
        "bill": wide["bill_rate"][home],
    }, index=years)
    fx_home = cast(pd.Series, fx[home])
    ok = nominal.notna().all(axis=1) & infl.notna() & fx_home.notna() & fx_home.shift(1).notna()
    real = (1.0 + nominal[ok]).div(1.0 + infl[ok], axis=0) - 1.0
    real = real.replace([np.inf, -np.inf], np.nan).dropna()
    return real[list(ASSETS)]
