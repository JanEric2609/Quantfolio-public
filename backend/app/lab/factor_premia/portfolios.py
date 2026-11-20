"""Monthly factor portfolios from the JKP characteristics panel, built in DuckDB.

Construction, per strategy (a simplified JKP / Fama-French recipe):

1. Universe: one row per ``(gvkey, eom)`` (the latest extract wins), in the
   region's countries, without micro and nano caps (``size_grp``), with a
   positive ``me`` and a next-month return ``ret_exc_lead1m``.
2. Within each country and month, rank the signal (percentile, ties shared);
   the top third is the long leg and the bottom third the short leg.
   Countries with fewer than ``MIN_STOCKS_PER_COUNTRY_MONTH`` ranked stocks
   that month are skipped.
3. Value-weight each leg (by ``me``) within a country, then combine countries
   weighted by their total market cap that month. The market is the
   value-weighted universe over the same countries.

Characteristics at ``eom`` are already lag-safe (see
``read_wrds_factor_characteristics``), and ``ret_exc_lead1m`` is the return
over the *following* month, so each row is an honest out-of-sample month.
Returns are JKP's USD excess returns. The long-short and "top third minus
market" differences are close to currency-neutral, because both legs are
weighted across the same countries.
"""
from __future__ import annotations

from pathlib import Path
from typing import cast

import pandas as pd

from app.foundation.data_engineering.panel_sql import (
    WRDS_FACTOR_CHARACTERISTICS_DIR,
    panel_partition_paths,
    query_panel,
)
from app.lab.factor_premia.strategies import (
    EXCLUDED_SIZE_GROUPS,
    MIN_STOCKS_PER_COUNTRY_MONTH,
    STRATEGIES,
)

SERIES_COLUMNS = ["strategy", "month", "top", "bottom", "market", "long_short", "long_only", "n_stocks"]


def _rank(expr: str, name: str) -> str:
    # Partitioning on "is null" keeps missing values out of the ranking.
    return (
        f"CASE WHEN ({expr}) IS NOT NULL THEN percent_rank() OVER "
        f"(PARTITION BY eom, excntry, ({expr}) IS NULL ORDER BY {expr}) END AS {name}"
    )


def _sql() -> str:
    # Formatted only from the module-level STRATEGIES constants, never from
    # input; every runtime value is a bound parameter.
    single = [s for s in STRATEGIES if s.signal]
    ranks = ",\n        ".join(_rank(s.signal, f"pr_{s.key}") for s in single)
    unions = "\n    UNION ALL ".join(
        f"SELECT '{s.key}' AS strategy, eom, excntry, me, r, pr_{s.key} AS pr FROM ranked"  # noqa: S608
        for s in single
    )
    return f"""
WITH scan AS (
    SELECT gvkey, eom, excntry, size_grp, me, be_me, mom_12_1, gp_at, at_gr1, rvol_21d,
           ret_exc_lead1m AS r, ingested_at
    FROM read_parquet(?, union_by_name = true)
    WHERE excntry IN (SELECT unnest(?))
),
latest AS (
    SELECT * FROM scan
    QUALIFY row_number() OVER (PARTITION BY gvkey, eom ORDER BY ingested_at DESC) = 1
),
universe AS (
    SELECT * FROM latest
    WHERE me > 0 AND r IS NOT NULL AND isfinite(r) AND r > -1
      AND coalesce(size_grp, '') NOT IN (SELECT unnest(?))
),
ranked AS (
    SELECT *,
        {ranks}
    FROM universe
),
combo AS (
    SELECT eom, excntry, me, r, (pr_value + pr_momentum) / 2 AS score
    FROM ranked WHERE pr_value IS NOT NULL AND pr_momentum IS NOT NULL
),
scored AS (
    {unions}
    UNION ALL SELECT 'value_momentum' AS strategy, eom, excntry, me, r,
        percent_rank() OVER (PARTITION BY eom, excntry ORDER BY score) AS pr
    FROM combo
),
counted AS (
    SELECT *, count(*) OVER (PARTITION BY strategy, eom, excntry) AS n
    FROM scored WHERE pr IS NOT NULL
),
legs AS (
    SELECT strategy, eom, excntry,
        sum(CASE WHEN pr >= 2.0 / 3 THEN me * r END) / sum(CASE WHEN pr >= 2.0 / 3 THEN me END) AS top,
        sum(CASE WHEN pr < 1.0 / 3 THEN me * r END) / sum(CASE WHEN pr < 1.0 / 3 THEN me END) AS bottom,
        count(*) AS n
    FROM counted WHERE n >= ?
    GROUP BY ALL
),
market AS (
    SELECT eom, excntry, sum(me * r) / sum(me) AS market, sum(me) AS cap
    FROM universe GROUP BY ALL
)
SELECT l.strategy, l.eom,
    sum(l.top * m.cap) / sum(m.cap) AS top,
    sum(l.bottom * m.cap) / sum(m.cap) AS bottom,
    sum(m.market * m.cap) / sum(m.cap) AS market,
    sum(l.n) AS n_stocks
FROM legs l JOIN market m USING (eom, excntry)
WHERE l.top IS NOT NULL AND l.bottom IS NOT NULL
GROUP BY ALL
ORDER BY l.strategy, l.eom
"""  # noqa: S608


def factor_series(panel_dir: Path, countries: tuple[str, ...]) -> pd.DataFrame:
    """Monthly leg returns for every pre-registered strategy.

    Returns one row per strategy and return month with ``top``, ``bottom``,
    ``market``, ``long_short`` (top - bottom), ``long_only`` (top - market)
    and ``n_stocks``. ``month`` is the month the return was earned in, one
    month after the characteristics' ``eom``. Empty if the panel is missing.
    """
    paths = panel_partition_paths(panel_dir, WRDS_FACTOR_CHARACTERISTICS_DIR)
    if not paths:
        return pd.DataFrame(columns=pd.Index(SERIES_COLUMNS))
    frame = query_panel(
        _sql(),
        [paths, list(countries), list(EXCLUDED_SIZE_GROUPS), MIN_STOCKS_PER_COUNTRY_MONTH],
    )
    if frame.empty:
        return pd.DataFrame(columns=pd.Index(SERIES_COLUMNS))
    frame["month"] = pd.to_datetime(frame["eom"]).dt.to_period("M") + 1
    frame["long_short"] = frame["top"] - frame["bottom"]
    frame["long_only"] = frame["top"] - frame["market"]
    frame["n_stocks"] = frame["n_stocks"].astype(int)
    return cast(pd.DataFrame, frame[SERIES_COLUMNS]).reset_index(drop=True)
