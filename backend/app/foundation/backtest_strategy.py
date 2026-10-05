"""Strategy backtesting — simple SMA/momentum/RSI/Bollinger strategies.

Extracted from ``services/quant.py`` so the core quant service (VaR, Monte Carlo,
frontier, factor exposure) is no longer coupled to strategy backtesting logic.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_strategy_backtest_cached(
    db: Any,
    ticker: str,
    strategy: str = "sma_cross",
    start: date | None = None,
    end: date | None = None,
    params: dict[str, Any] | None = None,
    days: int = 730,
) -> dict:
    """Backtest from the market-service price cache (preferred path)."""
    from app.foundation import market as market_service

    try:
        rows = market_service.history(db, ticker, days=days)
    except Exception:
        rows = []
    if start:
        rows = [row for row in rows if row["date"] >= start]
    if end:
        rows = [row for row in rows if row["date"] <= end]
    if len(rows) < 3:
        return unavailable_backtest(ticker, strategy, "No cached or fetched market history is available.")
    close = {row["date"]: row["close"] for row in rows}
    source = "price_cache"
    if any(not row.get("stale") and row.get("source") == "yfinance" for row in rows):
        source = "cache_then_yfinance"
    return strategy_backtest_from_prices(ticker, close, strategy=strategy, params=params or {}, source=source)


def strategy_backtest_from_prices(
    ticker: str,
    close: Any,
    *,
    strategy: str,
    params: dict[str, Any] | None = None,
    source: str = "fixture",
) -> dict:
    """Core backtest logic from a price series (no external dependency)."""
    import pandas as pd

    params = params or {}
    close = pd.Series(close).dropna().astype(float)
    if close.empty:
        return unavailable_backtest(ticker, strategy, "No close prices supplied.")
    warnings: list[str] = []
    if len(close) < 252:
        warnings.append("Less than one year of price history; confidence should be low.")
    returns = close.pct_change()
    returns.iloc[0] = 0.0
    signal = _strategy_signal(close, strategy, params)
    execution_signal = signal.shift(1).fillna(0)
    turnover_series = execution_signal.diff().abs().fillna(execution_signal.abs())
    transaction_cost_bps = float(params.get("transaction_cost_bps", 10))
    slippage_bps = float(params.get("slippage_bps", 5))
    total_cost = turnover_series * ((transaction_cost_bps + slippage_bps) / 10000)
    strategy_returns = execution_signal * returns - total_cost
    equity = (1 + strategy_returns).cumprod() * 10000
    buy_hold_equity = (close / close.iloc[0]) * 10000
    drawdown = equity / equity.cummax() - 1
    benchmark_returns = returns
    active_returns = strategy_returns - benchmark_returns
    risk_free_annual = float(params.get("risk_free", 0.0))
    volatility = strategy_returns.std() * math.sqrt(252)
    tracking_error = active_returns.std() * math.sqrt(252)
    # Delegates to quant_metrics for the canonical formula (ADR 0003 decision
    # 6) instead of a parallel hand-rolled one — same pattern already used
    # three lines below for historical_var_95.
    from app.foundation.quant_metrics import sharpe_ratio as _canonical_sharpe_ratio
    from app.foundation.quant_metrics import sortino_ratio as _canonical_sortino_ratio
    sharpe = _canonical_sharpe_ratio(strategy_returns.tolist(), risk_free=risk_free_annual, periods_per_year=252)
    sortino = _canonical_sortino_ratio(strategy_returns.tolist(), risk_free=risk_free_annual, periods_per_year=252)
    information_ratio = (
        0.0
        if tracking_error == 0 or math.isnan(tracking_error)
        else (active_returns.mean() * 252) / tracking_error
    )
    wins = strategy_returns[strategy_returns > 0].sum()
    losses = abs(strategy_returns[strategy_returns < 0].sum())
    trades = _trade_markers(signal)
    if not trades:
        warnings.append("Strategy produced no trades; treat as buy/hold or failed depending on intent.")
    turnover = float(turnover_series.sum())
    hit_rate = float((strategy_returns[strategy_returns != 0] > 0).mean()) if (strategy_returns != 0).any() else 0.0
    exposure_time = float((execution_signal > 0).mean())
    buy_hold_drawdown = buy_hold_equity / buy_hold_equity.cummax() - 1
    if abs(drawdown.min()) > 0.2 and abs(drawdown.min()) > abs(buy_hold_drawdown.min()):
        warnings.append("Strategy beats or tracks benchmark with drawdown above 20%; review or reject for risk.")
    return {
        "ticker": ticker,
        "strategy": strategy,
        "status": "completed",
        "source": source,
        "period": f"{_index_date(close.index[0])} to {_index_date(close.index[-1])}",
        "transaction_cost_assumption": f"{transaction_cost_bps:g} bps per turnover",
        "slippage_assumption": f"{slippage_bps:g} bps per turnover",
        "execution_note": "Signals are shifted one period to avoid same-day close-to-close lookahead.",
        "rebalance_frequency": params.get("rebalance_frequency", "signal driven"),
        "out_of_sample_period": params.get("out_of_sample_period", "not configured"),
        "warnings": warnings,
        "metrics": {
            "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1),
            "buy_hold_return": float(buy_hold_equity.iloc[-1] / buy_hold_equity.iloc[0] - 1),
            "sharpe": float(sharpe),
            "sortino": float(sortino),
            "max_drawdown": float(abs(drawdown.min())),
            "win_rate": float((strategy_returns > 0).mean()),
            "hit_rate": hit_rate,
            "profit_factor": float(wins / losses) if losses else 0.0,
            "cagr": float((equity.iloc[-1] / equity.iloc[0]) ** (252 / max(len(equity), 1)) - 1),
            "volatility": float(volatility) if not math.isnan(volatility) else 0.0,
            "historical_var_95": _historical_var(strategy_returns.tolist(), 0.95),
            "tracking_error": float(tracking_error) if not math.isnan(tracking_error) else 0.0,
            "information_ratio": float(information_ratio),
            "turnover": turnover,
            "exposure_time": exposure_time,
        },
        "benchmark_comparison": {
            "benchmark": "buy_hold_same_asset",
            "excess_return": float(
                (equity.iloc[-1] / equity.iloc[0] - 1) - (buy_hold_equity.iloc[-1] / buy_hold_equity.iloc[0] - 1)
            ),
            "tracking_error": float(tracking_error) if not math.isnan(tracking_error) else 0.0,
            "information_ratio": float(information_ratio),
            "relative_drawdown": float(abs(drawdown.min()) - abs(buy_hold_drawdown.min())),
        },
        "equity_curve": [
            {"date": _index_date(idx), "value": float(value)}
            for idx, value in equity.tail(520).items()
        ],
        "rolling_drawdown": [
            {"date": _index_date(idx), "value": float(value)}
            for idx, value in drawdown.tail(520).items()
        ],
        "rolling_volatility": [
            {"date": _index_date(idx), "value": float(value)}
            for idx, value in (strategy_returns.rolling(21).std() * math.sqrt(252)).dropna().tail(520).items()
        ],
        "trades": trades,
    }


def fallback_backtest_metrics() -> dict[str, float]:
    """Zeroed metrics dict for unavailable/failed backtests."""
    return {
        "total_return": 0.0,
        "buy_hold_return": 0.0,
        "sharpe": 0.0,
        "sortino": 0.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "cagr": 0.0,
        "volatility": 0.0,
        "historical_var_95": 0.0,
        "tracking_error": 0.0,
        "information_ratio": 0.0,
        "turnover": 0.0,
        "hit_rate": 0.0,
        "exposure_time": 0.0,
    }


def unavailable_backtest(ticker: str, strategy: str, message: str) -> dict:
    """Error response shape for a failed backtest."""
    return {
        "ticker": ticker,
        "strategy": strategy,
        "status": "unavailable",
        "message": message,
        "metrics": fallback_backtest_metrics(),
        "warnings": [message],
        "benchmark_comparison": {},
        "equity_curve": [],
        "rolling_drawdown": [],
        "rolling_volatility": [],
        "trades": [],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strategy_signal(close: Any, strategy: str, params: dict[str, Any]) -> Any:
    """Generate a trading signal Series from a close-price Series."""
    import pandas as pd

    signal = pd.Series(1.0, index=close.index)
    if strategy in ("buy_hold", "buy-and-hold"):
        return signal
    if strategy == "sma_cross":
        fast = int(params.get("fast", 50))
        slow = int(params.get("slow", 200))
        fast_ma = close.rolling(max(2, fast)).mean()
        slow_ma = close.rolling(max(fast + 1, slow)).mean()
        return (fast_ma > slow_ma).astype(float).fillna(0)
    if strategy == "rsi_mean_reversion":
        period = int(params.get("period", 14))
        low = float(params.get("oversold", 30))
        high = float(params.get("overbought", 70))
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = -delta.clip(upper=0).rolling(period).mean()
        rsi = 100 - (100 / (1 + gain / loss.replace(0, float("nan"))))
        return ((rsi < low) | ((rsi < high) & (rsi.shift(1) < low))).astype(float).fillna(0)
    if strategy == "momentum":
        lookback = int(params.get("lookback", 63))
        return (close.pct_change(lookback) > 0).astype(float).fillna(0)
    if strategy == "bollinger_breakout":
        window = int(params.get("window", 20))
        width = float(params.get("width", 2))
        mid = close.rolling(window).mean()
        upper = mid + width * close.rolling(window).std()
        breakout = (close > upper).astype(float)
        return breakout.where(breakout > 0).ffill().fillna(0)
    return signal


def _trade_markers(signal: Any) -> list[dict[str, Any]]:
    """Extract buy/sell markers from a signal Series."""
    changes = signal.diff().fillna(signal)
    trades: list[dict[str, Any]] = []
    for idx, value in changes[changes != 0].items():
        trades.append({"date": _index_date(idx), "side": "buy" if value > 0 else "sell"})
    return trades[-100:]


def _index_date(value: Any) -> str:
    """Format an index value (Timestamp | str) as ISO date string."""
    if hasattr(value, "date"):
        return value.date().isoformat()
    return str(value)


def _historical_var(returns: list[float], confidence: float = 0.95) -> float:
    """Historical VaR — delegates to the canonical implementation.

    This used to be an inlined copy ("zero imports from quant"), a
    deliberate isolation that ADR 0003 (decisions 1 and 6) overrides:
    single-source correctness outweighs the module-level import cost,
    since every real backtest path materialises pandas objects anyway.
    """
    from app.foundation.quant_metrics import historical_var as _canonical_historical_var

    return _canonical_historical_var(returns, confidence)
