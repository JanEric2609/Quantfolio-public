"""Point-in-time symbol joins for the four datasets loaded in ADR 0015 Phase 4:
CCM link quality, WRDS factor characteristics, IBES analyst estimates, and
SEC insider trading.

Mirrors :mod:`app.foundation.data_engineering.pit_fundamentals` exactly for the
two ``gvkey``-keyed sources (WRDS factor characteristics, CCM link quality):
the same ``symbol -> ISIN -> gvkey`` resolution via ``security_master`` and
``security_identifiers_pit``, so these two carry the same coverage/reliability
guarantees fundamentals already has. When that finds nothing (the security
master only knows securities a user has held or imported), US symbols fall
back to ``ticker -> permno -> gvkey`` through the CRSP name history
(``crsp_names_pit``) and the CCM link history, stamped ``crsp_ticker_ccm``.

**IBES and insider trading are different and weaker.** Neither has a WRDS
ICLINK extract loaded in this app (see ``ibes_estimates_schema``'s "ticker
trap" docstring), so there is no verified crosswalk to ``gvkey``/ISIN for
them. The only practical join here is a direct string match against the
ticker each source carries natively (IBES's ``oftic``, insider trading's own
``ticker``, both compared against the app *symbol* on upper-cased
alphanumerics only, so ``BRK-B`` / ``BRK.B`` / ``BRKB`` agree).
That match can silently miss (ticker changed) or
silently collide (ticker reused across delistings) -- every function here
that uses it stamps the result with ``"data_confidence": "ticker_match"`` so
callers and dossiers can distinguish it from the gvkey-verified sources
(``"data_confidence": "gvkey_verified"``).

**Look-ahead trap.** ``wrds_factors_schema.FACTOR_CHARACTERISTICS_COLUMNS``
includes ``ret_exc_lead1m`` -- JKP's own forward (t+1 month) excess return,
shipped for factor-return validation only. It is deliberately excluded from
:data:`FACTOR_CHARACTERISTIC_VALUE_COLUMNS` below; nothing in this module
ever reads it as a signal input.
"""
from __future__ import annotations

import logging
from typing import Any, cast

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.foundation.data_engineering.ccm_link_schema import (
    CCM_RESEARCH_QUALITY_LINKPRIMS,
    CCM_RESEARCH_QUALITY_LINKTYPES,
)
from app.foundation.data_engineering._pit_duckdb import (
    ccm_links_for_gvkey,
    ccm_links_for_permnos,
    crsp_names_for_symbol,
    ibes_estimates_for_oftic,
    insider_trading_coverage,
    insider_trading_for_ticker,
    insider_trailing_window_signal,
    security_identifiers_for_isin,
    symbol_match_key,
    symbol_match_keys,
    wrds_factor_characteristics_for_gvkeys,
)
from app.foundation.data_engineering.paths import get_panel_dir
from app.foundation.data_engineering.pit_fundamentals import resolve_isin_for_symbol
from app.foundation.providers.utils import strip_exchange_suffix

logger = logging.getLogger(__name__)

# Deliberately excludes ret_exc_lead1m (see module docstring) and the
# identifier/metadata columns (gvkey, permno, eom, excntry, size_grp).
FACTOR_CHARACTERISTIC_VALUE_COLUMNS: tuple[str, ...] = (
    "me",
    "mom_12_1",
    "be_me",
    "gp_at",
    "at_gr1",
)

# Standard annual-horizon, EPS-measure convention (module docstring of
# ibes_estimates_loader: "the standard choice for annual earnings-surprise
# research"). A caller wanting a different horizon/measure should add a new
# function rather than parameterise this one -- SUE/dispersion arithmetic
# below assumes EPS units throughout.
_IBES_FPI = "1"
_IBES_MEASURE = "EPS"

# Cohen/Malloy/Pomorski "routine" trader definition: 3+ consecutive prior
# years with a same-month, same-direction trade. Applied in
# ``pit_insider_signal_for_symbol`` to exclude noise before scoring.
_ROUTINE_MIN_CONSECUTIVE_YEARS = 3
_CLUSTER_WINDOW_DAYS = 30

# JKP characteristics are monthly and ``eom`` is already the availability
# date (see ``read_wrds_factor_characteristics``), so a fresh row is at most
# ~31 days old. A quarter leaves room for a late monthly refresh; anything
# older means the extract itself has gone stale.
_WRDS_MAX_STALENESS_DAYS = 92
_FLOW_WINDOW_DAYS = 90

# IBES summary statistics are monthly (``statpers``, the Thursday before the
# third Friday), so the same quarter-long tolerance as the WRDS
# characteristics above: a consensus older than that is a stopped extract
# or a dropped ticker, not the current view.
_IBES_MAX_STALENESS_DAYS = 92

# Form 4 is due two business days after the trade, so a refreshed extract
# trails the calendar by a few days; beyond this a trailing window reaches
# past what the extract covers.
_INSIDER_COVERAGE_GRACE_DAYS = 7


# The three "simple" per-symbol joins below (WRDS factors, IBES, insider
# trading) are each invoked once per symbol in a cross-sectional universe by
# app.lab.alphacrafter.panel.build_panel. They read through _pit_duckdb, which
# filters inside DuckDB's Parquet scan and returns only the symbol's slice
# (already deduped exactly like the loaders' read_* functions), so no call
# ever materialises a full-dataset pandas frame -- peak memory is bounded by
# one symbol's rows for any panel size. This replaced the byte-bounded
# cachetools caches of the full frames (#265), which capped accumulation but
# not the peak of the first full read; see ADR 0016's addendum and
# docs/archive/plans/2026-09-22-pit-join-duckdb-migration.md. No result cache: each
# symbol needs its own query anyway.


def _resolve_gvkey_as_of(db: Session, symbol: str, dates: pd.Index) -> pd.DataFrame | None:
    """Shared first hop of the gvkey-keyed joins: *dates* -> gvkey as of each date.

    Two routes, tried in order:

    1. ``symbol -> ISIN`` via this app's ``security_master``, then ISIN ->
       gvkey through ``security_identifiers_pit`` (Compustat Global) with the
       same ``pd.merge_asof(..., direction="backward")`` discipline as
       ``pit_fundamentals.pit_fundamentals_for_symbol``. Stamped
       ``gvkey_verified``.
    2. Only when (1) finds nothing and *symbol* carries no exchange suffix
       (i.e. a US listing): ``ticker -> permno`` through the CRSP name history
       (the name window must cover each date), then ``permno -> gvkey``
       through the research-quality CCM link history (the link window must
       cover it too). Stamped ``crsp_ticker_ccm``. The security master only
       holds securities this app has resolved for a user (holdings/imports),
       so without this route no universe stock ever reaches its gvkey.

    Returns a frame with ``orig_date``, ``date`` (tz-naive working column),
    ``gvkey`` and ``data_confidence``, or ``None`` when neither route yields a
    gvkey -- "no opinion, not confirmed absent", as before.
    """
    original_index = pd.DatetimeIndex(dates)
    naive_dates = (
        original_index.tz_localize(None) if original_index.tz is not None else original_index
    )
    left = pd.DataFrame({"orig_date": original_index, "date": naive_dates}).sort_values("date")

    isin = resolve_isin_for_symbol(db, symbol)
    if isin is not None:
        identifiers = security_identifiers_for_isin(get_panel_dir(), isin)
        if not identifiers.empty:
            identifiers = identifiers.sort_values("as_of_date")[["as_of_date", "gvkey"]]
            linked = pd.merge_asof(
                left, identifiers, left_on="date", right_on="as_of_date", direction="backward"
            )
            linked["data_confidence"] = "gvkey_verified"
            return linked

    if not symbol or strip_exchange_suffix(symbol) != symbol:
        return None
    gvkeys = _gvkeys_via_crsp_ticker(symbol, cast(pd.Series, left["date"]))
    if gvkeys is None:
        return None
    linked = left.assign(gvkey=pd.array(gvkeys, dtype="string"))
    linked["data_confidence"] = "crsp_ticker_ccm"
    return linked


def _covering(starts: Any, ends: Any, when: pd.Series) -> Any:
    """Boolean matrix ``[len(when), len(starts)]``: row window covers the date.

    A null end is open-ended (still current / still active).
    """
    d = when.to_numpy(dtype="datetime64[ns]")[:, None]
    start = starts.to_numpy(dtype="datetime64[ns]")[None, :]
    end = ends.to_numpy(dtype="datetime64[ns]", na_value=np.datetime64("NaT"))[None, :]
    return (start <= d) & (np.isnat(end) | (d <= end))


def _gvkeys_via_crsp_ticker(symbol: str, when: pd.Series) -> list[Any] | None:
    """Per-date gvkey (or ``None``) via CRSP names -> CCM; ``None`` if no date resolves.

    A date is left unresolved rather than guessed when the ticker maps to more
    than one permno on it, or the permno to more than one gvkey among its best
    (``P`` before ``C``) primary links.
    """
    names = crsp_names_for_symbol(get_panel_dir(), symbol)
    if names.empty:
        return None
    name_hits = _covering(names["namedt"], names["nameenddt"], when)
    permnos = names["permno"].to_numpy(dtype="float64")

    links = ccm_links_for_permnos(get_panel_dir(), names["permno"].dropna().unique().tolist())
    links = cast(
        pd.DataFrame,
        links[
            links["linktype"].isin(CCM_RESEARCH_QUALITY_LINKTYPES)
            & links["linkprim"].isin(CCM_RESEARCH_QUALITY_LINKPRIMS)
        ],
    )
    if links.empty:
        return None
    link_hits = _covering(links["linkdt"], links["linkenddt"], when)
    link_permnos = links["permno"].to_numpy(dtype="float64")
    link_gvkeys = links["gvkey"].astype(str).to_numpy()
    link_rank = np.array([CCM_RESEARCH_QUALITY_LINKPRIMS.index(p) for p in links["linkprim"].tolist()])

    out: list[Any] = []
    for i in range(len(when)):
        candidates = set(permnos[name_hits[i]].tolist())
        if len(candidates) != 1:
            out.append(None)
            continue
        permno = candidates.pop()
        mask = link_hits[i] & (link_permnos == permno)
        if not mask.any():
            out.append(None)
            continue
        best = link_rank[mask].min()
        gvkeys = set(link_gvkeys[mask & (link_rank == best)].tolist())
        out.append(gvkeys.pop() if len(gvkeys) == 1 else None)
    return out if any(g is not None for g in out) else None


def pit_ccm_link_quality_for_symbol(
    db: Session, symbol: str, as_of: pd.Timestamp | str
) -> dict[str, Any] | None:
    """Research-quality CCM link flag for *symbol* as of *as_of*.

    CCM is infrastructure here, not a user-facing signal: it validates the
    ``gvkey`` the other PIT joins already trust, rather than adding a new
    one. Returns ``None`` when *symbol* has no resolvable gvkey (same
    coverage caveat as ``pit_fundamentals_for_symbol``) or no CCM link row
    covers *as_of* at all.
    """
    linked = _resolve_gvkey_as_of(db, symbol, pd.DatetimeIndex([pd.Timestamp(as_of)]))
    gvkey_col = cast("pd.Series", linked["gvkey"]) if linked is not None else None
    if gvkey_col is None or gvkey_col.isna().all():
        return None
    gvkey = gvkey_col.iloc[0]
    assert linked is not None
    data_confidence = str(cast("pd.Series", linked["data_confidence"]).iloc[0])

    cutoff = cast(pd.Timestamp, pd.Timestamp(as_of))
    links = ccm_links_for_gvkey(get_panel_dir(), gvkey)
    active = (links["linkdt"] <= cutoff) & (links["linkenddt"].isna() | (links["linkenddt"] >= cutoff))
    links = cast(pd.DataFrame, links[active])
    if links.empty:
        return None

    research_quality = bool(
        links["linktype"].isin(CCM_RESEARCH_QUALITY_LINKTYPES).any()
        and links["linkprim"].isin(CCM_RESEARCH_QUALITY_LINKPRIMS).any()
    )
    permnos = sorted({int(p) for p in links["permno"].dropna().unique().tolist()})
    return {
        "gvkey": gvkey,
        "permnos": permnos,
        "research_quality_link": research_quality,
        "data_confidence": data_confidence,
    }


def pit_wrds_factor_characteristics_for_symbol(
    db: Session, symbol: str, dates: pd.Index
) -> pd.DataFrame | None:
    """Point-in-time WRDS/JKP factor characteristics for *symbol*.

    Same two-hop ``merge_asof`` chain as ``pit_fundamentals_for_symbol``
    (dates -> gvkey via ISIN, then gvkey's most recent characteristics row as
    of each date via ``eom``), just against
    :func:`read_wrds_factor_characteristics`'s rows instead of ``read_fundamentals``
    (read via :func:`wrds_factor_characteristics_for_gvkeys`, filtered in the scan).
    Returns a DataFrame indexed by *dates* with ``gvkey`` and
    :data:`FACTOR_CHARACTERISTIC_VALUE_COLUMNS`, or ``None`` when *symbol* has
    no resolvable gvkey / no characteristics coverage at all.

    A characteristics row older than :data:`_WRDS_MAX_STALENESS_DAYS` on a
    given date is not carried forward: that date's values come back NaN
    (reported unavailable), rather than a months-old snapshot silently
    posing as current once the loaded extract stops being refreshed.
    ``data_confidence`` is ``gvkey_verified`` (ISIN route) or
    ``crsp_ticker_ccm`` (CRSP ticker route) -- see ``_resolve_gvkey_as_of``.
    """
    linked = _resolve_gvkey_as_of(db, symbol, dates)
    if linked is None:
        return None

    gvkeys = sorted({g for g in linked["gvkey"].dropna().unique().tolist()})
    if not gvkeys:
        return None

    characteristics = wrds_factor_characteristics_for_gvkeys(get_panel_dir(), gvkeys)
    if characteristics.empty:
        return None
    characteristics = characteristics.sort_values("eom")

    columns = ["gvkey", "eom", *FACTOR_CHARACTERISTIC_VALUE_COLUMNS]
    joined = pd.merge_asof(
        linked.sort_values("date"),
        characteristics[columns],
        left_on="date",
        right_on="eom",
        by="gvkey",
        direction="backward",
        tolerance=cast(pd.Timedelta, pd.Timedelta(days=_WRDS_MAX_STALENESS_DAYS)),
    )
    joined = joined.set_index("orig_date").reindex(pd.DatetimeIndex(dates))
    joined["data_confidence"] = linked["data_confidence"].iloc[0]
    return cast(pd.DataFrame, joined[["gvkey", *FACTOR_CHARACTERISTIC_VALUE_COLUMNS, "data_confidence"]])


def pit_ibes_estimate_signal_for_symbol(
    db: Session, symbol: str, dates: pd.Index
) -> pd.DataFrame | None:
    """Point-in-time IBES analyst-estimate signal for *symbol*.

    Best-effort ticker match on ``oftic`` (see module docstring) -- there is
    no verified crosswalk to this app's own symbols. ``oftic`` never carries
    this app's yfinance-style exchange suffix (WRDS's IBES International file
    gives ``"RWE"``, not ``"RWE.DE"``), so *symbol* is normalised with
    :func:`strip_exchange_suffix` before matching -- a no-op for US tickers,
    which carry no suffix to begin with. Computes, per requested date, the
    consensus snapshot last known as of that date (``statpers``, never look
    ahead):

    - ``sue``: standardized unexpected earnings, ``(actual - meanest) /
      stdev`` -- only populated on the snapshot row where the period's
      ``actual`` has been announced; NaN otherwise. A known simplification:
      this does not yet realign FPI horizon at the earnings-announcement
      boundary (the estimate "rolls" to a new fiscal year once the old one
      resolves), so treat SUE coverage as sparse rather than exhaustive.
    - ``revision_momentum``: fractional change in ``meanest`` versus the
      immediately preceding snapshot for the same ticker/fpi/measure --
      positive means analysts raised estimates since last snapshot.
    - ``dispersion``: ``stdev / abs(meanest)``, a forecast-uncertainty flag.

    Returns ``None`` when no IBES row matches *symbol* on ``oftic`` at all.
    """
    if not symbol:
        return None

    match_key = symbol_match_key(strip_exchange_suffix(symbol))
    if not match_key:
        return None
    estimates = ibes_estimates_for_oftic(get_panel_dir(), match_key, _IBES_FPI, _IBES_MEASURE)
    ticker_match = cast(pd.DataFrame, estimates[symbol_match_keys(cast(pd.Series, estimates["oftic"])) == match_key])
    if ticker_match.empty:
        return None

    history = ticker_match.sort_values("statpers").copy()
    # Only compare a consensus with the previous one for the SAME fiscal
    # period. FPI 1 rolls to the next fiscal year once the old one ends, and
    # across the roll the change is next year's EPS against this year's:
    # LRCX's June year-end read as a +65% "revision" in August 2026.
    history["meanest_prior"] = history.groupby(["ticker", "fpedats"], dropna=False)["meanest"].shift(1)
    history["revision_momentum"] = (
        (history["meanest"] - history["meanest_prior"]) / history["meanest_prior"].abs()
    )
    history["dispersion"] = history["stdev"] / history["meanest"].abs()
    history["sue"] = pd.Series(pd.NA, index=history.index, dtype="Float64")
    has_actual = history["actual"].notna()
    history.loc[has_actual, "sue"] = (
        history.loc[has_actual, "actual"] - history.loc[has_actual, "meanest"]
    ) / history.loc[has_actual, "stdev"]

    original_index = pd.DatetimeIndex(dates)
    naive_dates = (
        original_index.tz_localize(None) if original_index.tz is not None else original_index
    )
    left = pd.DataFrame({"orig_date": original_index, "date": naive_dates}).sort_values("date")

    columns = ["statpers", "sue", "revision_momentum", "dispersion"]
    joined = pd.merge_asof(
        left,
        history[columns],
        left_on="date",
        right_on="statpers",
        direction="backward",
        tolerance=cast(pd.Timedelta, pd.Timedelta(days=_IBES_MAX_STALENESS_DAYS)),
    )
    joined = joined.set_index("orig_date").reindex(original_index)
    joined["data_confidence"] = "ticker_match"
    return cast(pd.DataFrame, joined[["sue", "revision_momentum", "dispersion", "data_confidence"]])


def _classify_routine_insiders(open_market: pd.DataFrame) -> pd.Series:
    """Boolean mask, True where a (cik, transaction_code, calendar-month) row
    belongs to an insider with 3+ consecutive prior years trading that same
    month/direction -- the Cohen/Malloy/Pomorski "routine" filter. These
    carry no predictive power and are excluded from the opportunistic signal
    below, though the raw filing is never dropped from the caller's view.
    """
    if open_market.empty:
        return pd.Series(dtype=bool)

    month = open_market["transaction_date"].dt.month
    year = open_market["transaction_date"].dt.year
    key = list(zip(open_market["cik"], open_market["transaction_code"], month, strict=False))
    grouped_years: dict[tuple[Any, Any, int], set[int]] = {}
    for k, y in zip(key, year, strict=False):
        grouped_years.setdefault(k, set()).add(int(y))

    def _is_routine(k: tuple, y: int) -> bool:
        years = grouped_years[k]
        # 3+ distinct years up to and including this one, each one apart.
        consecutive = sorted(yr for yr in years if yr <= y)
        streak = 1
        for i in range(len(consecutive) - 1, 0, -1):
            if consecutive[i] - consecutive[i - 1] == 1:
                streak += 1
                if streak >= _ROUTINE_MIN_CONSECUTIVE_YEARS:
                    return True
            else:
                break
        return streak >= _ROUTINE_MIN_CONSECUTIVE_YEARS

    return pd.Series(
        [_is_routine(k, y) for k, y in zip(key, year, strict=False)], index=open_market.index
    )


def _planned_trades(rows: pd.DataFrame) -> pd.Series:
    """True where the filing ticked the Rule 10b5-1 plan box (``plan_10b5_1``).

    A plan's trades were scheduled in advance, and since the 2023 amendment
    (cooling-off periods, no overlapping or single-trade plans) plan sales
    are markedly less opportunistic (Kim, Kim & Rajgopal, JAE 2026). The
    routine filter alone misses them: it needs three years of history, and
    a new plan has none. Loaded from SEC's 2026 Q2 set, 45% of open-market
    sale rows and 4% of purchase rows came from ticked filings. Unknown (filings before
    April 2023, partitions without the column) is not treated as a plan.
    """
    if "plan_10b5_1" not in rows.columns:
        return pd.Series(False, index=rows.index)
    return cast(pd.Series, rows["plan_10b5_1"]).eq(True).fillna(False).astype(bool)


def pit_insider_signal_for_symbol(
    db: Session, symbol: str, dates: pd.Index
) -> pd.DataFrame | None:
    """Point-in-time insider-trading signal for *symbol*.

    Best-effort ticker match against the issuer's own ``ticker`` field (see
    module docstring) -- no verified crosswalk exists. Point-in-time anchor
    is ``filing_date``, never ``transaction_date`` (insider_trading_schema's
    own look-ahead warning). Restricted to open-market P/S transactions
    (:data:`OPEN_MARKET_TRANSACTION_CODES`), with routine traders
    (:func:`_classify_routine_insiders`) and trades from filings that ticked
    the Rule 10b5-1 plan box (:func:`_planned_trades`) excluded.

    Per requested date, over the trailing windows below (as of that date):

    - ``cluster_buy_score``: distinct opportunistic insiders (``cik``)
      making open-market purchases in the trailing :data:`_CLUSTER_WINDOW_DAYS`.
    - ``net_insider_flow_usd``: dollar-weighted opportunistic buys minus
      sells (``shares * price_per_share``) in the trailing
      :data:`_FLOW_WINDOW_DAYS`.

    Returns ``None`` when no insider-trading row matches *symbol* at all.
    """
    match_key = symbol_match_key(symbol)
    if not match_key:
        return None

    raw = insider_trading_for_ticker(get_panel_dir(), match_key)
    ticker_match = cast(pd.DataFrame, raw[symbol_match_keys(cast(pd.Series, raw["ticker"])) == match_key])
    if ticker_match.empty:
        return None

    ticker_match = ticker_match.dropna(subset=["filing_date", "transaction_date"]).copy()
    routine = _classify_routine_insiders(ticker_match).reindex(ticker_match.index, fill_value=False)
    opportunistic = cast(pd.DataFrame, ticker_match[~(routine | _planned_trades(ticker_match))])

    original_index = pd.DatetimeIndex(dates)
    naive_dates = (
        original_index.tz_localize(None) if original_index.tz is not None else original_index
    )
    cluster, flow = insider_trailing_window_signal(
        opportunistic, naive_dates, _CLUSTER_WINDOW_DAYS, _FLOW_WINDOW_DAYS
    )
    cluster_values = pd.Series(cluster, dtype="int64")
    flow_values = pd.Series(flow, dtype="float64")
    # An empty window only means "no insider trading" while the extract still
    # covers it. Past its last filing the signal is unknown, not zero: prod's
    # extract ends 2024-03-29, and on 2026-09-28 all 133 matched US names
    # read cluster 0 / flow 0 and were scored as neutral evidence.
    coverage = insider_trading_coverage(get_panel_dir())
    if coverage is not None:
        last_filing = coverage[1] + pd.Timedelta(days=_INSIDER_COVERAGE_GRACE_DAYS)
        uncovered = np.asarray(naive_dates > last_filing, dtype=bool)
        if uncovered.any():
            cluster_values = cluster_values.astype("float64")
            cluster_values[uncovered] = np.nan
            flow_values[uncovered] = np.nan
    return pd.DataFrame(
        {
            "cluster_buy_score": cluster_values,
            "net_insider_flow_usd": flow_values,
            "data_confidence": "ticker_match",
        }
    ).set_axis(pd.DatetimeIndex(original_index.to_list(), name="orig_date"))


# Column groups reused by pit_ml_features_for_symbol below -- kept next to
# the three join functions it composes so the field lists can't drift apart
# from what those functions actually return.
_IBES_VALUE_COLUMNS: tuple[str, ...] = ("sue", "revision_momentum", "dispersion")
_INSIDER_VALUE_COLUMNS: tuple[str, ...] = ("cluster_buy_score", "net_insider_flow_usd")


def pit_ml_features_for_symbol(db: Session, symbol: str, dates: pd.Index) -> pd.DataFrame:
    """Compose all three "simple" PIT-join sources above (WRDS factor
    characteristics, IBES estimates, insider trading) into one per-row ML
    feature frame indexed exactly like *dates* -- the shape
    ``app.lab.quant_ml.features.build_features``'s ``extra_features``
    parameter expects.

    This is a genuine per-row, point-in-time join (each function above
    already resolves its value as of each requested date via
    ``merge_asof(..., direction="backward")`` or an explicit trailing-window
    loop) -- never a single current snapshot broadcast across every row. See
    the ``regime_snapshot`` incident documented on
    ``app.lab.quant_ml.features.augment_with_regime_features`` for what this
    function deliberately avoids repeating.

    Unlike the three functions it composes, this one never returns ``None``
    and never raises: a missing/unmatched source becomes NaN value columns
    plus a ``0.0`` availability flag for that source (``wrds_factor_available``
    / ``ibes_estimate_available`` / ``insider_signal_available``, 1.0 where
    that row has a real value from that source), not a dropped row or a
    propagated exception. Downstream NaN handling (imputation) is the
    caller's responsibility -- see ``app.lab.quant_ml.pipelines.build_pipeline``.
    """
    index = pd.DatetimeIndex(dates)
    out = pd.DataFrame(index=index)

    sources: list[tuple[Any, tuple[str, ...], str]] = [
        (pit_wrds_factor_characteristics_for_symbol, FACTOR_CHARACTERISTIC_VALUE_COLUMNS, "wrds_factor_available"),
        (pit_ibes_estimate_signal_for_symbol, _IBES_VALUE_COLUMNS, "ibes_estimate_available"),
        (pit_insider_signal_for_symbol, _INSIDER_VALUE_COLUMNS, "insider_signal_available"),
    ]
    for fetch_fn, value_columns, flag_col in sources:
        try:
            joined = fetch_fn(db, symbol, index)
            if joined is not None:
                values = {col: joined[col] for col in value_columns}
                availability = joined[list(value_columns)].notna().any(axis=1).astype(float)
            else:
                values = None
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "pit_ml_features_for_symbol: %s failed for %s: %s", fetch_fn.__name__, symbol, exc
            )
            values = None

        if values is None:
            for col in value_columns:
                out[col] = float("nan")
            out[flag_col] = 0.0
        else:
            for col, series in values.items():
                out[col] = series
            out[flag_col] = availability

    return out
