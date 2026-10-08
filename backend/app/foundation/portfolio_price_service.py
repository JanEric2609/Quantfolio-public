"""Service for building portfolio price matrices and computing common derived quantities.

Consolidates logic previously duplicated across 15+ endpoints in ``api/quant.py``
into a single stateless ``@dataclass``.  Every method computes on demand — no
internal caching (FastAPI handlers are request-scoped, so recomputation is fine).

Usage::

    svc = PortfolioPriceService(db, user.id)
    matrix, weights, diagnostics = svc.price_matrix()
    returns = svc.weighted_returns(matrix, weights)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.foundation.live_positions import (
    LivePosition,
    live_positions,
    manual_holdings_filter,
    set_position_ticker,
)
from app.foundation.models.entities import Holding, Portfolio
from app.foundation import market as market_service
from app.foundation.eur_prices import to_eur
from app.foundation.fx_rates import convert as fx_convert
from app.foundation.quant import price_matrix_to_returns

logger = logging.getLogger(__name__)

# A line missing on a day (holiday in its market) keeps its last close for at most this many calendar days.
MAX_FILL_DAYS = 3

# Default ETF proxy tickers for the four classic factor-mimicking portfolios.
# Injectable via the constructor for unit tests.
DEFAULT_FACTOR_PROXIES: dict[str, str] = {
    "market": "EUNL.DE",
    "size": "IUSN.DE",
    "value": "IWVL.L",
    "momentum": "IS3R.DE",
}


def _synced_weight(pos: LivePosition) -> float:
    """A synced position's weight: market value, else cost when the broker reports no price."""
    value = float(pos.value)
    if value > 0:
        return value
    if pos.avg_buy_price is not None:
        return float(pos.quantity) * float(pos.avg_buy_price)
    return 0.0


@dataclass
class PortfolioPriceService:
    """Stateless service that builds price matrices and derived quantities.

    Parameters
    ----------
    db:
        Active SQLAlchemy session.
    user_id:
        UUID of the portfolio owner.
    factor_proxies:
        Mapping of factor name → ETF ticker.  Falls back to
        ``DEFAULT_FACTOR_PROXIES`` when ``None``.
    """

    db: Session
    user_id: str
    factor_proxies: dict[str, str] | None = None

    # ── helpers ──────────────────────────────────────────────────────────

    def _get_factor_proxies(self) -> dict[str, str]:
        return self.factor_proxies if self.factor_proxies is not None else DEFAULT_FACTOR_PROXIES

    # ── Core: price matrix ───────────────────────────────────────────────

    def price_matrix(
        self,
    ) -> tuple[dict[str, dict[str, float]], dict[str, float], dict[str, Any]]:
        """Build a price matrix and weight dict from the user's portfolio.

        Returns a 3-tuple::

            (matrix, weights, diagnostics)

        * **matrix** — ``{ticker: {date (isoformat): close in EUR}}``
        * **weights** — ``{ticker: market value in EUR}``
        * **diagnostics** — metadata (priced_assets, missing_history, …)

        Closes are restated in EUR on each day's rate (``eur_prices``), so a
        USD line carries its currency return, as it does for an investor in
        EUR. A listing without an FX rate is left out (``missing_fx``) rather
        than mixed in as if its dollars were euros. Every weight is a market
        value: a manual holding is valued at its latest close, at cost only
        when no close exists.
        """
        from app.foundation.portfolio.isin_resolver import _try_resolve_isin

        matrix: dict[str, dict[str, float]] = {}
        weights: dict[str, float] = {}
        missing_history: list[str] = []
        # Tickers whose history fetch came back empty. A manual holding listed
        # as missing only for lack of a cost must not keep a synced position of
        # the same ticker out of the matrix.
        history_failed: set[str] = set()
        missing_fx: list[str] = []
        rate_cache: dict[str, dict[str, float] | None] = {}

        def eur_history(ticker: str) -> dict[str, float]:
            bars = market_service.history(self.db, ticker, days=730)
            closes = {
                str(r["date"])[:10]: float(r["close"])
                for r in bars or []
                if r.get("close") is not None and float(r["close"]) > 0
            }
            converted, _ccy = to_eur(self.db, ticker, closes, rate_cache=rate_cache)
            if closes and not converted:
                missing_fx.append(ticker)
            return converted

        # --- 1. Manual Holding rows (ticker already set) ---
        # The synced copies of broker positions are read as positions in step 2
        # at market value; counting them here too would weigh them twice.
        holdings = (
            self.db.query(Holding)
            .join(Portfolio)
            .filter(Portfolio.user_id == self.user_id, Holding.ticker.is_not(None), manual_holdings_filter())
            .all()
        )
        for holding in holdings:
            ticker = (holding.ticker or "").upper()
            if not ticker or ticker in matrix:
                continue
            try:
                closes = eur_history(ticker)
            except Exception:
                logger.debug("History fetch failed for %s", ticker)
                closes = {}
            if not closes:
                missing_history.append(ticker)
                history_failed.add(ticker)
                continue
            matrix[ticker] = closes
            last_close = closes[max(closes)]
            if last_close > 0:
                weights[ticker] = float(holding.quantity) * last_close
            elif holding.avg_buy_price is not None:
                raw_weight = float(holding.quantity) * float(holding.avg_buy_price)
                weights[ticker] = fx_convert(raw_weight, (holding.currency or "EUR").upper(), "EUR", self.db)
            else:
                weights[ticker] = 0.0

        # --- 2. Synced broker positions, DKB and Scalable (may have ticker or only ISIN) ---
        dkb_positions = live_positions(self.db, self.user_id)
        dkb_isin_only_count = 0
        for pos in dkb_positions:
            ticker = (pos.ticker or "").upper()
            if not ticker and pos.isin:
                resolved = _try_resolve_isin(pos.isin, self.db)
                if resolved:
                    ticker = resolved.upper()
                    set_position_ticker(self.db, pos, resolved)
                    try:
                        self.db.commit()
                    except Exception:
                        self.db.rollback()
            if not ticker:
                dkb_isin_only_count += 1
                continue
            if ticker in matrix:
                # The same instrument at a second depot (or also held by hand).
                weights[ticker] = weights.get(ticker, 0.0) + _synced_weight(pos)
                continue
            if ticker in history_failed:
                continue
            try:
                closes = eur_history(ticker)
            except Exception:
                logger.debug("History fetch failed for %s position %s (%s)", pos.source, ticker, pos.isin)
                closes = {}
            if not closes:
                if ticker not in missing_history:
                    missing_history.append(ticker)
                history_failed.add(ticker)
                continue
            matrix[ticker] = closes
            weights[ticker] = _synced_weight(pos)

        total_holdings = len(holdings) + len(dkb_positions)
        # The dkb_* keys predate Scalable; they count the synced positions at
        # every broker and keep their names for the API contract.
        diagnostics = {
            "priced_assets": sorted(matrix.keys()),
            "manual_ticker_holdings": len(holdings),
            "dkb_position_count": len(dkb_positions),
            "missing_history": missing_history,
            "missing_fx": sorted(set(missing_fx)),
            "currency": "EUR",
            "partial_data": bool(missing_history) and bool(matrix),
            "dkb_positions_without_symbol_mapping": dkb_isin_only_count,
            "message": (
                f"{dkb_isin_only_count} synced position(s) have no ticker mapping "
                "— add ISIN overrides in Settings to include them."
                if dkb_isin_only_count
                else (
                    f"Using {len(matrix)}/{total_holdings} positions with available price data."
                    if missing_history
                    else f"Using {len(matrix)} position(s) from manual holdings and broker sync."
                )
            ),
        }
        return matrix, weights, diagnostics

    # ── Derived helpers ──────────────────────────────────────────────────

    def to_frame(self, matrix: dict[str, dict[str, float]]) -> pd.DataFrame:
        """Convert a price matrix dict to a wide ``DataFrame`` (dates × tickers).

        Dates become the index. Wholly-empty ticker columns are dropped, then
        rows are restricted to dates where every remaining ticker has a real
        price — no forward-fill. Forward-filling a gap before ``.pct_change()``
        fabricates a spurious 0% return for the gap day(s) (non-synchronous/
        stale-trading bias); see ``quant.price_matrix_to_returns`` for detail.

        The index is normalized to a genuine ``DatetimeIndex`` (matching
        ``quant.price_matrix_to_returns``'s convention) so that a returns
        series derived from this frame (``weighted_returns()``) joins
        correctly against ``factor_proxy_returns()``'s output in
        ``factor_exposures()`` — an inner join between a ``Timestamp``-typed
        index and a ``str``-typed one never matches, even for the same
        calendar date.
        """
        frame = pd.DataFrame(matrix).dropna(axis=1, how="all").dropna()
        frame.index = pd.to_datetime(frame.index)
        return frame

    def weighted_returns(
        self,
        matrix: dict[str, dict[str, float]],
        weights: dict[str, float],
    ) -> pd.Series:
        """Weighted daily portfolio return **Series** from a price matrix + weights.

        This is the most-duplicated 5-line pattern in the codebase (Pattern A):
        ``pd.DataFrame → to_frame (strict overlap) → pct_change → mul(weights) → sum``.

        Returns a ``pd.Series`` indexed by date with the daily portfolio return.
        """
        frame = self.to_frame(matrix)
        weight_series = pd.Series(weights).reindex(frame.columns).fillna(0)
        total = weight_series.sum()
        if total > 0:
            weight_series = weight_series / total
        return frame.pct_change().dropna().mul(weight_series, axis=1).sum(axis=1)

    @staticmethod
    def returns_list_static(
        matrix: dict[str, dict[str, float]],
        weights: dict[str, float],
    ) -> tuple[list[float], list[str]]:
        """Raw portfolio returns as flat **lists** (avoids pandas dependency).

        Static variant — usable without a ``PortfolioPriceService`` instance.
        This is the implementation shared by the instance method and the
        legacy router helper ``_portfolio_returns_series``.
        """
        if not matrix or not weights:
            return [], []
        total_weight = sum(max(0.0, w) for w in weights.values())
        if total_weight <= 0:
            return [], []
        normalised = {t: max(0.0, weights.get(t, 0.0)) / total_weight for t in matrix}
        # Every line is carried forward over the union of all trading days (at
        # most MAX_FILL_DAYS calendar days), not cut down to the dates all of
        # them share: a holiday in one market would otherwise turn the next
        # portfolio "daily" return into a multi-day return while the
        # benchmark's return on that date stays a single day. Only dates that
        # another line trades on are filled, so this is a holiday carry, not a
        # gap filler. Trade-off: the filled day shows a 0 % return for that
        # line (stale-price bias, Fisher 1966), which slightly lowers measured
        # volatility; quant.price_matrix_to_returns refuses to fill at all
        # and drops such dates, which is right for covariance but not for a
        # series that must be paired date by date with a benchmark.
        grid = sorted({d for prices in matrix.values() for d in prices})
        if not grid:
            return [], []
        first_all = max(min(prices) for prices in matrix.values() if prices)
        grid = [d for d in grid if d >= first_all]
        filled: dict[str, dict[str, float]] = {}
        for ticker, prices in matrix.items():
            last_date: date | None = None
            last_px: float | None = None
            out: dict[str, float] = {}
            for d in grid:
                px = prices.get(d)
                if px:
                    last_date, last_px = date.fromisoformat(d[:10]), px
                    out[d] = px
                elif last_px is not None and last_date is not None and (date.fromisoformat(d[:10]) - last_date).days <= MAX_FILL_DAYS:
                    out[d] = last_px
            filled[ticker] = out
        series: list[float] = []
        dates: list[str] = []
        for prev, cur in zip(grid, grid[1:]):
            period_return = 0.0
            ok = True
            for ticker, px in filled.items():
                p0, p1 = px.get(prev), px.get(cur)
                if not p0 or not p1:
                    ok = False
                    break
                period_return += normalised[ticker] * (p1 / p0 - 1)
            if ok:
                series.append(period_return)
                dates.append(cur)
        return series, dates

    def returns_list(
        self,
        matrix: dict[str, dict[str, float]],
        weights: dict[str, float],
    ) -> tuple[list[float], list[str]]:
        """Instance-method wrapper around :meth:`returns_list_static`."""
        return self.returns_list_static(matrix, weights)

    # ── Factor proxy helpers (Pattern B) ─────────────────────────────────

    def factor_proxy_returns(self) -> pd.DataFrame:
        """Daily return **DataFrame** for all configured factor proxies.

        Eliminates Pattern B — the factor proxy fetching + reshaping block that
        appears in ``/factor-model``, ``/factors/dashboard``, ``/factors/rotation``,
        and ``/factors/factor-attribution``.

        Returns a ``pd.DataFrame`` with columns named after each factor.
        """
        proxies = self._get_factor_proxies()
        prices: dict[str, dict[str, float]] = {}
        for name, ticker in proxies.items():
            rows = market_service.history(self.db, ticker, days=730)
            if rows:
                prices[name] = {row["date"].isoformat(): row["close"] for row in rows}
        if not prices:
            return pd.DataFrame()
        return price_matrix_to_returns(prices)

    def factor_proxy_prices(self) -> dict[str, dict[str, float]]:
        """Raw close-price dict for all configured factor proxies.

        Used by endpoints that need the original price levels (not returns),
        such as the smart-beta tracking error computation.
        """
        proxies = self._get_factor_proxies()
        prices: dict[str, dict[str, float]] = {}
        for name, ticker in proxies.items():
            rows = market_service.history(self.db, ticker, days=730)
            if rows:
                prices[name] = {row["date"].isoformat(): row["close"] for row in rows}
        return prices


def portfolio_price_matrix(
    db,
    user_id: str,
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """Convenience (matrix, weights) view over :meth:`PortfolioPriceService.price_matrix`.

    Canonical home of the helper formerly defined in ``app.interface.api.quant._common``
    (as the private ``_portfolio_price_matrix``); services and the API layer
    both consume it from here.
    """
    matrix, weights, _diagnostics = PortfolioPriceService(db, user_id).price_matrix()
    return matrix, weights
