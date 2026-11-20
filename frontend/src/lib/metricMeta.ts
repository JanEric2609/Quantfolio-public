import { formatCurrency, formatNumber, formatPercent } from "./format";

export interface MetricMeta {
  label: string;
  unit: "ratio" | "percent" | "days" | "count" | "float" | "currency";
  decimals: number;
  glossaryKey?: string;
  description: string;
  severity?: {
    good: (v: number) => boolean;
    warn: (v: number) => boolean;
    invert?: boolean;
  };
}

export const METRIC_META: Record<string, MetricMeta> = {
  sharpe: {
    label: "Sharpe Ratio",
    unit: "ratio",
    decimals: 2,
    glossaryKey: "sharpe",
    description: "Excess return per unit of total volatility. Higher is better; >1 is good, >2 is very good.",
    severity: {
      good: (v) => v > 1.4,
      warn: (v) => v > 0.8,
    },
  },
  sortino: {
    label: "Sortino Ratio",
    unit: "ratio",
    decimals: 2,
    glossaryKey: "sortino",
    description: "Like Sharpe but only counts downside volatility. Penalises only losses, not gains.",
    severity: {
      good: (v) => v > 1.4,
      warn: (v) => v > 0.8,
    },
  },
  calmar: {
    label: "Calmar Ratio",
    unit: "ratio",
    decimals: 2,
    glossaryKey: "calmar",
    description: "Annual return divided by max drawdown. Higher means stronger gain-to-pain.",
    severity: {
      good: (v) => v > 1.4,
      warn: (v) => v > 0.8,
    },
  },
  beta: {
    label: "Beta",
    unit: "float",
    decimals: 2,
    glossaryKey: "beta",
    description: "Sensitivity to the benchmark. 1.0 moves like the market; >1 amplifies; <1 dampens.",
    severity: {
      good: (v) => v >= 0.8 && v <= 1.2,
      warn: (v) => v >= 0.5 && v <= 1.5,
    },
  },
  alpha: {
    label: "Alpha",
    unit: "percent",
    decimals: 2,
    glossaryKey: "alpha",
    description:
      "Jensen's alpha: the yearly return not explained by the benchmark, the intercept of the daily regression × 252. " +
      "Only meaningful when |t| is above about 2; below that it is noise.",
    severity: {
      good: (v) => v > 0,
      warn: () => false,
    },
  },
  r_squared: {
    label: "R\u00b2",
    unit: "float",
    decimals: 3,
    glossaryKey: "r_squared",
    // Neutral on purpose: R\u00b2 says how closely the book follows the benchmark, not how good it is.
    description: "Share of portfolio variance explained by the benchmark (0\u20131). Not a quality score: a book that is the benchmark scores 1.",
  },
  information_ratio: {
    label: "Information ratio",
    unit: "ratio",
    decimals: 2,
    description: "Yearly return above the benchmark per unit of tracking error. Above 0.5 sustained over years is rare.",
  },
  treynor: {
    label: "Treynor Ratio",
    unit: "ratio",
    decimals: 2,
    glossaryKey: "treynor",
    description: "Return per unit of systematic (beta) risk.",
    severity: {
      good: (v) => v > 0.1,
      warn: (v) => v > 0.05,
    },
  },
  annualised_return: {
    label: "Annualised Return",
    unit: "percent",
    decimals: 2,
    glossaryKey: "annualised_return",
    description: "The geometric average yearly return of the portfolio. Represents the constant annual rate that would produce the same cumulative return over the measurement period.",
    severity: {
      good: (v) => v > 0,
      warn: () => false,
    },
  },
  annualised_volatility: {
    label: "Annualised Volatility",
    unit: "percent",
    decimals: 2,
    glossaryKey: "annualised_volatility",
    description: "The standard deviation of the portfolio's annualised returns. A measure of risk and price fluctuation. Lower values indicate a more stable portfolio.",
    severity: {
      good: (v) => v < 0.1,
      warn: (v) => v < 0.2,
      invert: true,
    },
  },
  max_drawdown: {
    label: "Max Drawdown",
    unit: "percent",
    decimals: 2,
    glossaryKey: "max_drawdown",
    description: "Worst peak-to-trough loss during the period.",
    severity: {
      good: (v) => v > -0.1,
      warn: (v) => v > -0.2,
      invert: true,
    },
  },
  cvar_95: {
    label: "CVaR 95%",
    unit: "percent",
    decimals: 2,
    glossaryKey: "cvar_95",
    description: "Average loss on the worst 5% of days.",
    severity: {
      good: (v) => v < 0.02,
      warn: (v) => v < 0.05,
      invert: true,
    },
  },
  var_95: {
    label: "VaR 95%",
    unit: "percent",
    decimals: 2,
    glossaryKey: "var_95",
    description: "Daily loss expected to be exceeded only 5% of the time.",
    severity: {
      good: (v) => v < 0.02,
      warn: (v) => v < 0.05,
      invert: true,
    },
  },
  var_99: {
    label: "VaR 99%",
    unit: "percent",
    decimals: 2,
    glossaryKey: "var_99",
    description: "Daily loss expected to be exceeded only 1% of the time.",
    severity: {
      good: (v) => v < 0.03,
      warn: (v) => v < 0.06,
      invert: true,
    },
  },
  skewness: {
    label: "Skewness",
    unit: "float",
    decimals: 3,
    glossaryKey: "skewness",
    description: "Measures asymmetry of the return distribution. Negative skew means more frequent negative returns. Zero means symmetric distribution.",
    severity: {
      good: (v) => Math.abs(v) < 0.5,
      warn: (v) => Math.abs(v) < 1,
    },
  },
  kurtosis: {
    label: "Kurtosis (Excess)",
    unit: "float",
    decimals: 3,
    glossaryKey: "kurtosis",
    description: "Measures the tailedness of the return distribution. Excess kurtosis of 0 means normal distribution. Positive values indicate fatter tails (more extreme outcomes).",
    severity: {
      good: (v) => Math.abs(v) < 1,
      warn: (v) => Math.abs(v) < 2,
    },
  },
  drawdown_duration: {
    label: "Drawdown Duration",
    unit: "days",
    decimals: 0,
    glossaryKey: "drawdown_duration",
    description: "How many trading days the portfolio stayed underwater.",
    severity: {
      good: (v) => v < 30,
      warn: (v) => v < 90,
      invert: true,
    },
  },
  volatility: {
    label: "Volatility",
    unit: "percent",
    decimals: 2,
    glossaryKey: "volatility",
    description: "Standard deviation of returns, annualised. The primary measure of portfolio risk. Higher volatility implies greater price fluctuation and uncertainty.",
    severity: {
      good: (v) => v < 0.15,
      warn: (v) => v < 0.25,
      invert: true,
    },
  },
  downside_deviation: {
    label: "Downside Deviation",
    unit: "percent",
    decimals: 2,
    glossaryKey: "downside_deviation",
    description: "A measure of downside risk that focuses on negative returns only. Unlike standard deviation, it penalises only losses, not gains.",
    severity: {
      invert: true,
      good: () => false,
      warn: () => false,
    },
  },
  tracking_error: {
    label: "Tracking Error",
    unit: "percent",
    decimals: 2,
    glossaryKey: "tracking_error",
    description: "The standard deviation of the difference between portfolio and benchmark returns. Lower tracking error indicates the portfolio closely follows its benchmark.",
    severity: {
      good: (v) => v < 0.05,
      warn: (v) => v < 0.1,
      invert: true,
    },
  },
  info_ratio: {
    label: "Information Ratio",
    unit: "ratio",
    decimals: 2,
    glossaryKey: "info_ratio",
    description: "Portfolio active return divided by tracking error. Measures risk-adjusted outperformance relative to a benchmark. Higher values indicate more consistent outperformance.",
    severity: {
      good: (v) => v > 0.5,
      warn: (v) => v > 0,
    },
  },
  win_rate: {
    label: "Win Rate",
    unit: "percent",
    decimals: 1,
    glossaryKey: "win_rate",
    description: "The percentage of trades that generated a positive return. Above 50% indicates more winning than losing trades, but must be considered alongside average win/loss size.",
    severity: {
      good: (v) => v > 0.5,
      warn: () => false,
    },
  },
  profit_factor: {
    label: "Profit Factor",
    unit: "ratio",
    decimals: 2,
    glossaryKey: "profit_factor",
    description: "Gross profits divided by gross losses. A value above 1.0 indicates profitability. Above 1.5 is considered good for most trading strategies.",
    severity: {
      good: (v) => v > 1.5,
      warn: (v) => v > 1.0,
    },
  },
  effective_hhi: {
    label: "Effective HHI",
    unit: "float",
    decimals: 4,
    glossaryKey: "hhi",
    description: "Actual concentration after expanding ETF constituents (look-through). Lower means better diversification.",
    severity: {
      good: (v) => v < 0.1,
      warn: (v) => v < 0.15,
    },
  },
  simple_hhi: {
    label: "Simple HHI",
    unit: "float",
    decimals: 4,
    glossaryKey: "hhi",
    description: "Concentration measured on positions only, without ETF look-through. Can overstate risk for ETF-heavy portfolios.",
    severity: {
      good: (v) => v < 0.1,
      warn: (v) => v < 0.15,
    },
  },
  lookthrough_count: {
    label: "Underlying Positions",
    unit: "count",
    decimals: 0,
    description: "Total unique holdings after expanding ETF constituents. Higher count = broader diversification.",
    severity: {
      good: (v) => v > 200,
      warn: (v) => v > 50,
    },
  },
  diversification_ratio: {
    label: "Diversification Ratio",
    unit: "ratio",
    decimals: 2,
    glossaryKey: "diversification_ratio",
    description: "Weighted average volatility of individual assets divided by portfolio volatility. Values above 1.0 indicate diversification benefit.",
    severity: {
      good: (v) => v > 1.5,
      warn: (v) => v > 1.0,
    },
  },
  mc_median_return: {
    label: "MC Median Return",
    unit: "percent",
    decimals: 2,
    description: "Median annualised return from Monte Carlo simulation. The central scenario out of thousands of simulated paths.",
    severity: {
      good: (v) => v > 0.05,
      warn: (v) => v > 0,
    },
  },
  // Regime tab (WI-6)
  vix: {
    label: "VIX",
    unit: "float",
    decimals: 1,
    glossaryKey: "vix_indicator",
    description: "CBOE Volatility Index — 30-day implied S&P 500 volatility. Below 20 is calm; 20–30 is elevated; above 30 triggers the crisis gate.",
    severity: {
      good: (v) => v < 20,
      warn: (v) => v < 30,
      invert: true,
    },
  },
  yield_spread: {
    label: "Yield Spread (10y–2y)",
    unit: "percent",
    decimals: 1,
    glossaryKey: "yield_spread",
    description: "US Treasury 10y minus 2y yield. Positive = normal curve (growth expected). Negative = inversion, a historically reliable recession warning.",
    severity: {
      good: (v) => v > 0,
      warn: () => false,
    },
  },

  assets_count: {
    label: "Assets",
    unit: "count",
    decimals: 0,
    description: "Number of distinct holdings with usable price history included in the correlation/factor analysis.",
  },
  factors_count: {
    label: "Factors",
    unit: "count",
    decimals: 0,
    description: "Number of risk factors the portfolio is regressed against (e.g. market, size, value, momentum).",
  },
  pair_correlation: {
    label: "Pair Correlation",
    unit: "float",
    decimals: 3,
    description: "Latest rolling correlation between the two selected assets. +1 moves together, 0 is unrelated, −1 moves opposite. Lower pairwise correlation improves diversification.",
  },
  factor_exposure: {
    label: "Factor Exposure",
    unit: "float",
    decimals: 3,
    description: "Sensitivity of the portfolio's returns to this factor. Positive means a tilt toward the factor; near zero means little exposure.",
  },
  technical_signals: {
    label: "Technical Signals",
    unit: "float",
    decimals: 2,
    description: "Trend/momentum indicators (e.g. moving-average crossovers, RSI) summarised as bullish / neutral / bearish for each factor or asset.",
  },
  factor_rotation: {
    label: "Factor Rotation",
    unit: "percent",
    decimals: 1,
    description: "Trailing-window returns per factor across 30/60/90 days, used to see which factors are leading or lagging right now.",
  },
  smart_beta: {
    label: "Smart-Beta Comparison",
    unit: "percent",
    decimals: 2,
    description: "Factor ETFs compared by tracking error vs the factor and overlap with your current holdings — a shortlist for cheap factor exposure.",
  },
  allocation_effect: {
    label: "Allocation Effect",
    unit: "percent",
    decimals: 3,
    description: "Brinson allocation: the active return from over/under-weighting sectors vs the benchmark, independent of stock picking.",
  },
  selection_effect: {
    label: "Selection Effect",
    unit: "percent",
    decimals: 3,
    description: "Brinson selection: the active return from picking better (or worse) securities than the benchmark within each sector.",
  },
  interaction_effect: {
    label: "Interaction Effect",
    unit: "percent",
    decimals: 3,
    description: "Brinson interaction: the combined effect of allocation and selection decisions interacting — small when one dominates.",
  },
};

export function getMetricSeverity(
  meta: MetricMeta,
  value: number | null | undefined,
): "good" | "warn" | "bad" | "neutral" {
  if (value == null || isNaN(value) || !meta.severity) return "neutral";
  if (meta.severity.good(value)) return meta.severity.invert ? "bad" : "good";
  if (meta.severity.warn(value)) return "warn";
  return meta.severity.invert ? "good" : "bad";
}

export function formatMetricValue(
  key: string,
  value: number | null | undefined,
): string {
  const meta = METRIC_META[key];
  if (value == null || isNaN(value)) return "\u2014";
  if (!meta) return String(value);
  if (meta.unit === "percent") return formatPercent(value, { digits: meta.decimals });
  if (meta.unit === "currency") return formatCurrency(value);
  if (meta.unit === "days") return formatNumber(Math.round(value), { digits: 0 });
  if (meta.unit === "count") return formatNumber(Math.round(value), { digits: 0 });
  return formatNumber(value, { digits: meta.decimals });
}
