"""Cross-sectional factor panel assembly for the AlphaCrafter Miner.

A *panel* is a mapping ``{variable -> DataFrame}`` where each DataFrame is
indexed by date (rows) and has one column per symbol. It is the shared input to
both :mod:`app.lab.alphacrafter.factor_dsl` (DSL formulas) and the seed
``quant_factors.compute_factor`` path.

Price/volume series come from :class:`BarStore`. Fundamentals
(``pe_ratio``/``market_cap``/``roe``) prefer the point-in-time Compustat path
(:mod:`app.foundation.data_engineering.pit_fundamentals`) — per-date values,
resolved via the symbol's ISIN (security_master) and the ISIN's gvkey as of
each date — and fall back to a single latest-snapshot broadcast from the
providers registry only for symbols that path does not (yet) cover.

**Point-in-time caveat, now partial:** a symbol only gets real history once
it has been resolved to an ISIN in ``security_master`` *and* that ISIN
appears in the ingested Compustat identifiers extract. Until then it keeps
the old behaviour — the provider registry's current snapshot broadcast
across the whole date index, correct for cross-sectional ranking today but
**not** survivorship-/lookahead-free historically. Treat fundamental-factor
IC as indicative only for any symbol not covered by the PIT path.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.foundation.data_backbone.bars import BarStore
from app.foundation.data_engineering.pit_fundamentals import (
    FUNDAMENTAL_VALUE_COLUMNS,
    pit_fundamentals_for_symbol,
)
from app.foundation.data_engineering.pit_panel_joins import (
    FACTOR_CHARACTERISTIC_VALUE_COLUMNS,
    pit_ibes_estimate_signal_for_symbol,
    pit_insider_signal_for_symbol,
    pit_wrds_factor_characteristics_for_symbol,
)

logger = logging.getLogger(__name__)

# All variables the DSL/panel may expose. Fundamental columns are always present
# (filled with NaN when unavailable) so DSL formulas referencing them do not
# raise — the resulting NaNs are dropped downstream in IC computation.
PRICE_FIELDS = ("open", "high", "low", "close", "volume")
FUNDAMENTAL_FIELDS = ("pe_ratio", "market_cap", "roe")
# gvkey-verified, same reliability as FUNDAMENTAL_FIELDS (see pit_panel_joins
# module docstring). ret_exc_lead1m is deliberately excluded upstream.
WRDS_FACTOR_FIELDS = FACTOR_CHARACTERISTIC_VALUE_COLUMNS
# Ticker-matched only, no verified crosswalk -- see pit_panel_joins module
# docstring's "data_confidence" caveat before treating these as equal-quality
# to the gvkey-verified fields above.
IBES_ESTIMATE_FIELDS = ("sue", "revision_momentum", "dispersion")
INSIDER_SIGNAL_FIELDS = ("cluster_buy_score", "net_insider_flow_usd")


class _Registry(Protocol):
    def get_fundamentals(self, symbol: str) -> dict[str, Any]: ...


def _extract_fundamentals(data: dict[str, Any]) -> dict[str, float | None]:
    """Pull pe_ratio/market_cap/roe out of a provider payload.

    Handles both the flat shape (finnhub/alphavantage: ``{"pe_ratio": ...}``)
    and EOD Historical Data's nested ``Highlights`` block.
    """
    if not isinstance(data, dict):
        return {"pe_ratio": None, "market_cap": None, "roe": None}

    highlights = data.get("Highlights") if isinstance(data.get("Highlights"), dict) else {}

    def pick(*keys: str) -> float | None:
        for source in (data, highlights):
            if source is None:
                continue
            for key in keys:
                val = source.get(key)
                if val not in (None, ""):
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        continue
        return None

    return {
        "pe_ratio": pick("pe_ratio", "PERatio"),
        "market_cap": pick("market_cap", "MarketCapitalization"),
        "roe": pick("roe", "ReturnOnEquityTTM"),
    }


def _populate_pit_series(
    db: Session,
    reference_columns: pd.Index,
    reference_index: pd.Index,
    fetch_fn: Any,
    value_columns: tuple[str, ...],
) -> dict[str, dict[str, pd.Series]]:
    """Shared per-symbol PIT-join loop for the three "simple" (no derived-math)
    datasets below — unlike fundamentals (pe_ratio/market_cap/roe, which are
    computed from raw accounting figures + the close price), these are used
    as-is straight from their ``pit_panel_joins`` join function.
    """
    series: dict[str, dict[str, pd.Series]] = {f: {} for f in value_columns}
    for symbol in reference_columns:
        try:
            joined = fetch_fn(db, symbol, reference_index)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("build_panel: PIT join failed for %s via %s: %s", symbol, fetch_fn.__name__, exc)
            joined = None
        if joined is None or bool(joined[list(value_columns)].isna().to_numpy().all()):
            continue
        for field in value_columns:
            series[field][symbol] = joined[field].reindex(reference_index)
    return series


def build_panel(
    db: Session,
    universe: list[str],
    start: datetime,
    end: datetime,
    *,
    registry: _Registry | None = None,
    include_fundamentals: bool = True,
    include_wrds_factors: bool = True,
    include_ibes_estimates: bool = True,
    include_insider_signals: bool = True,
) -> dict[str, pd.DataFrame]:
    """Assemble a (date x symbol) panel for ``universe`` over ``[start, end]``.

    Args:
        db: Database session.
        universe: Symbols forming the cross-section.
        start: Inclusive start of the price window.
        end: Inclusive end of the price window.
        registry: Provider registry (injectable for tests). When ``None`` and
            ``include_fundamentals`` is set, a real registry is built from ``db``.
        include_fundamentals: When ``False``, fundamental columns are all-NaN and
            no provider calls are made.
        include_wrds_factors: When ``False``, :data:`WRDS_FACTOR_FIELDS` are
            all-NaN and no CCM/WRDS-factor-characteristics lookups are made.
        include_ibes_estimates: When ``False``, :data:`IBES_ESTIMATE_FIELDS`
            are all-NaN and no IBES lookups are made.
        include_insider_signals: When ``False``, :data:`INSIDER_SIGNAL_FIELDS`
            are all-NaN and no insider-trading lookups are made.

    Returns:
        Mapping with every key in ``PRICE_FIELDS + FUNDAMENTAL_FIELDS +
        WRDS_FACTOR_FIELDS + IBES_ESTIMATE_FIELDS + INSIDER_SIGNAL_FIELDS``.
        Returns an empty dict if no symbol had price data.
    """
    bar_store = BarStore(db)
    field_series: dict[str, dict[str, pd.Series]] = {f: {} for f in PRICE_FIELDS}

    for symbol in universe:
        bars = bar_store.get_bars(symbol=symbol, start=start, end=end)
        if bars is None or bars.empty:
            continue
        indexed = bars.set_index("ts").sort_index()
        for field in PRICE_FIELDS:
            if field in indexed.columns:
                field_series[field][symbol] = pd.Series(
                    pd.to_numeric(indexed[field], errors="coerce")
                )

    close_map = field_series["close"]
    if not close_map:
        logger.warning("build_panel: no price data for any symbol in universe")
        return {}

    panel: dict[str, pd.DataFrame] = {}
    for field in PRICE_FIELDS:
        panel[field] = pd.DataFrame(field_series[field]).sort_index()

    # Align every price field onto the union calendar of the close frame.
    reference_index = panel["close"].index
    reference_columns = panel["close"].columns
    for field in PRICE_FIELDS:
        panel[field] = panel[field].reindex(index=reference_index, columns=reference_columns)

    # Fundamentals: point-in-time series per symbol where security_master +
    # the Compustat identifiers extract resolve it, one broadcast scalar per
    # symbol (the old behaviour) everywhere else.
    fundamentals_series: dict[str, dict[str, pd.Series]] = {f: {} for f in FUNDAMENTAL_FIELDS}
    fundamentals_scalar: dict[str, dict[str, float | None]] = {}
    pit_covered: set[str] = set()

    if include_fundamentals:
        for symbol in reference_columns:
            try:
                pit = pit_fundamentals_for_symbol(db, symbol, reference_index)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("build_panel: PIT fundamentals failed for %s: %s", symbol, exc)
                pit = None
            if pit is None or bool(pit[list(FUNDAMENTAL_VALUE_COLUMNS)].isna().to_numpy().all()):
                continue
            close_series = panel["close"][symbol]
            eps = pit["eps"].reindex(reference_index).replace(0, np.nan)
            equity = pit["common_equity"].reindex(reference_index).replace(0, np.nan)
            fundamentals_series["pe_ratio"][symbol] = close_series / eps
            fundamentals_series["market_cap"][symbol] = (
                close_series * pit["shares_outstanding"].reindex(reference_index)
            )
            fundamentals_series["roe"][symbol] = pit["net_income"].reindex(reference_index) / equity
            pit_covered.add(symbol)

        remaining = [s for s in reference_columns if s not in pit_covered]
        if remaining:
            if registry is None:
                try:
                    from app.foundation.providers.registry import build_provider_registry

                    registry = build_provider_registry(db)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("build_panel: could not build provider registry: %s", exc)
                    registry = None
            if registry is not None:
                for symbol in remaining:
                    try:
                        result = registry.get_fundamentals(symbol)
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.debug("build_panel: fundamentals failed for %s: %s", symbol, exc)
                        result = {}
                    data = result.get("data", {}) if isinstance(result, dict) else {}
                    fundamentals_scalar[symbol] = _extract_fundamentals(data)

    for field in FUNDAMENTAL_FIELDS:
        frame = pd.DataFrame(index=reference_index)
        for symbol in reference_columns:
            if symbol in fundamentals_series[field]:
                frame[symbol] = fundamentals_series[field][symbol].to_numpy()
            else:
                value = fundamentals_scalar.get(symbol, {}).get(field)
                frame[symbol] = np.full(len(reference_index), value, dtype=float)
        panel[field] = frame.reindex(columns=reference_columns)

    # WRDS factor characteristics / IBES estimates / insider trading: no
    # provider-registry scalar fallback exists for these (unlike
    # fundamentals above) -- a symbol without PIT/ticker-match coverage
    # simply gets an all-NaN column, same as the module docstring's
    # documented convention for DSL formulas referencing an unavailable field.
    simple_sources: list[tuple[bool, Any, tuple[str, ...]]] = [
        (include_wrds_factors, pit_wrds_factor_characteristics_for_symbol, WRDS_FACTOR_FIELDS),
        (include_ibes_estimates, pit_ibes_estimate_signal_for_symbol, IBES_ESTIMATE_FIELDS),
        (include_insider_signals, pit_insider_signal_for_symbol, INSIDER_SIGNAL_FIELDS),
    ]
    for enabled, fetch_fn, value_columns in simple_sources:
        series = (
            _populate_pit_series(db, reference_columns, reference_index, fetch_fn, value_columns)
            if enabled
            else {f: {} for f in value_columns}
        )
        for field in value_columns:
            frame = pd.DataFrame(index=reference_index)
            for symbol in reference_columns:
                if symbol in series[field]:
                    frame[symbol] = series[field][symbol].to_numpy()
                else:
                    frame[symbol] = np.full(len(reference_index), np.nan, dtype=float)
            panel[field] = frame.reindex(columns=reference_columns)

    return panel
