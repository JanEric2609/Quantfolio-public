from __future__ import annotations

from datetime import datetime
from operator import itemgetter
from typing import Sequence

import pandas as pd

from app.decision.engine.types import PerformanceSnapshot
from app.decision.engine import metrics as quant_metrics

MIN_OBSERVATIONS: int = 20  # minimum daily returns for annualised risk metrics

TRADING_DAYS_BY_PERIOD: dict[str, int] = {
    "1D": 1,
    "1W": 5,
    "1M": 21,
    "3M": 63,
    "6M": 126,
    "1Y": 252,
}

PERIODS: list[str] = list(TRADING_DAYS_BY_PERIOD.keys())

PERIODS_FULL: list[str] = [
    "1D", "1W", "1M", "3M", "6M", "1Y",
]

TRADING_DAYS_BY_PERIOD_FULL: dict[str, int] = {
    "1D": 1, "1W": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252,
}


class PerformanceTracker:
    def compute_from_nav_history(
        self,
        nav_history: list[dict],
        benchmark_returns: Sequence[float] | None = None,
        risk_free: float = 0.0,
        periods_per_year: int = 252,
    ) -> PerformanceSnapshot:
        if not nav_history:
            return PerformanceSnapshot(
                timestamp=datetime.utcnow(),
                total_value={},
                returns={p: 0.0 for p in PERIODS_FULL},
                metrics={},
            )

        sorted_history = sorted(
            self._normalize_timestamps(nav_history), key=itemgetter("timestamp")
        )
        latest = sorted_history[-1]
        timestamp = latest.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        elif not isinstance(timestamp, datetime):
            timestamp = datetime.utcnow()

        total_value: dict[str, float] = latest.get("total_value", {})
        if not total_value:
            total_value = {"EUR": 0.0}

        primary_currency = next(iter(total_value))
        values = [
            float(s["total_value"][primary_currency])
            for s in sorted_history
            if s.get("total_value") and primary_currency in s["total_value"]
        ]

        if len(values) < 2:
            return PerformanceSnapshot(
                timestamp=timestamp,
                total_value=total_value,
                returns={p: 0.0 for p in PERIODS_FULL},
                metrics={},
            )

        returns = self.compute_returns_from_values(values)

        period_returns: dict[str, float] = {}
        for period, days in TRADING_DAYS_BY_PERIOD_FULL.items():
            period_returns[period] = self._cumulative_return(returns, days)

        full_metrics = self._compute_full_metrics(
            returns, benchmark_returns, risk_free, periods_per_year,
        )

        for period, days in TRADING_DAYS_BY_PERIOD_FULL.items():
            trailing = returns[-days:] if len(returns) >= days else []
            if not trailing:
                continue
            win = len(trailing)
            # Always include cheap per-period metrics.
            full_metrics[f"cagr_{period}"] = quant_metrics.annualised_return(
                trailing, periods_per_year,
            )
            dd = quant_metrics.max_drawdown(trailing)
            full_metrics[f"max_drawdown_{period}"] = dd["max_drawdown"]
            # Gate annualised risk metrics behind MIN_OBSERVATIONS.
            if win < MIN_OBSERVATIONS:
                continue
            full_metrics[f"sharpe_{period}"] = quant_metrics.sharpe_ratio(
                trailing, risk_free, periods_per_year,
            )
            full_metrics[f"sortino_{period}"] = quant_metrics.sortino_ratio(
                trailing, risk_free, periods_per_year,
            )
            full_metrics[f"volatility_{period}"] = quant_metrics.annualised_volatility(
                trailing, periods_per_year,
            )
            if benchmark_returns is not None:
                bm = list(benchmark_returns)
                bm_aligned = bm[-win:] if len(bm) >= win else []
                if len(bm_aligned) == win:
                    full_metrics[f"beta_{period}"] = quant_metrics.beta(trailing, bm_aligned)
                    full_metrics[f"alpha_{period}"] = quant_metrics.alpha(
                        trailing, bm_aligned, risk_free, periods_per_year,
                    )

        return PerformanceSnapshot(
            timestamp=timestamp,
            total_value=total_value,
            returns=period_returns,
            metrics=full_metrics,
        )

    def compute_returns_from_values(self, values: Sequence[float]) -> list[float]:
        if len(values) < 2:
            return []
        result: list[float] = []
        for i in range(1, len(values)):
            prev = values[i - 1]
            if prev == 0:
                result.append(0.0)
                continue
            result.append((values[i] - prev) / prev)
        return result

    def compute_returns(self, nav_history: list[dict]) -> list[float]:
        if not nav_history:
            return []
        sorted_history = sorted(
            self._normalize_timestamps(nav_history), key=itemgetter("timestamp")
        )
        first = sorted_history[0]
        total_value = first.get("total_value", {})
        if not total_value:
            return []
        primary_currency = next(iter(total_value))
        values = [
            float(s["total_value"][primary_currency])
            for s in sorted_history
            if s.get("total_value") and primary_currency in s["total_value"]
        ]
        return self.compute_returns_from_values(values)

    def rolling_window_metrics(
        self,
        returns: Sequence[float],
        window_days: int,
        benchmark_returns: Sequence[float] | None = None,
        risk_free: float = 0.0,
        periods_per_year: int = 252,
    ) -> dict[str, float]:
        if window_days <= 0:
            return {}
        rs = [float(r) for r in returns]
        if len(rs) < window_days:
            return {}
        trailing = rs[-window_days:]
        metrics: dict[str, float] = {
            "sharpe": quant_metrics.sharpe_ratio(trailing, risk_free, periods_per_year),
            "sortino": quant_metrics.sortino_ratio(trailing, risk_free, periods_per_year),
            "calmar": quant_metrics.calmar_ratio(trailing, periods_per_year),
            "volatility": quant_metrics.annualised_volatility(trailing, periods_per_year),
            "cagr": quant_metrics.annualised_return(trailing, periods_per_year),
        }
        dd = quant_metrics.max_drawdown(trailing)
        metrics["max_drawdown"] = dd["max_drawdown"]
        metrics["max_drawdown_duration"] = dd["max_drawdown_duration"]

        if benchmark_returns is not None:
            bm = list(benchmark_returns)
            bm_aligned = bm[-window_days:] if len(bm) >= window_days else []
            if len(bm_aligned) == window_days:
                metrics["beta"] = quant_metrics.beta(trailing, bm_aligned)
                metrics["alpha"] = quant_metrics.alpha(
                    trailing, bm_aligned, risk_free, periods_per_year,
                )
        return metrics

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_timestamps(nav_history: list[dict]) -> list[dict]:
        normalized = []
        for entry in nav_history:
            entry = dict(entry)
            ts = entry.get("timestamp")
            if isinstance(ts, str):
                entry["timestamp"] = pd.to_datetime(ts)
            normalized.append(entry)
        return normalized

    @staticmethod
    def _cumulative_return(returns: Sequence[float], days: int) -> float:
        rs = list(returns)
        if not rs or days < 1:
            return 0.0
        segment = rs[-days:] if len(rs) >= days else rs
        cumulative = 1.0
        for r in segment:
            cumulative *= 1.0 + r
        return cumulative - 1.0

    @staticmethod
    def _compute_full_metrics(
        returns: list[float],
        benchmark_returns: Sequence[float] | None,
        risk_free: float,
        periods_per_year: int,
    ) -> dict[str, float]:
        metrics: dict[str, float] = {}
        n = len(returns)

        if n < 2:
            return metrics

        # Always include these: cheap and meaningful even on a handful of points.
        dd = quant_metrics.max_drawdown(returns)
        metrics["max_drawdown"] = dd["max_drawdown"]
        metrics["max_drawdown_duration"] = dd["max_drawdown_duration"]
        metrics["cagr"] = quant_metrics.annualised_return(returns, periods_per_year)

        if n < MIN_OBSERVATIONS:
            # Too few observations for reliable annualised risk metrics.
            return metrics

        metrics["sharpe"] = quant_metrics.sharpe_ratio(returns, risk_free, periods_per_year)
        metrics["sortino"] = quant_metrics.sortino_ratio(returns, risk_free, periods_per_year)
        metrics["calmar"] = quant_metrics.calmar_ratio(returns, periods_per_year)
        metrics["volatility"] = quant_metrics.annualised_volatility(returns, periods_per_year)

        if benchmark_returns is not None:
            bm = list(benchmark_returns)
            if len(bm) >= 2:
                metrics["beta"] = quant_metrics.beta(returns, bm)
                metrics["alpha"] = quant_metrics.alpha(returns, bm, risk_free, periods_per_year)

        return metrics
