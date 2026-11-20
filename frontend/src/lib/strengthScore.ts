import type { PiotroskiSignal } from "./api";
import { formatCompact, formatNumber, formatPercent } from "./format";

/** A strength-score criterion's value in its own unit: ROE 1.46 is 146 %, not "1.46"; D/E 590.21 is 590 %. */
export function formatSignalValue(s: Pick<PiotroskiSignal, "value" | "unit">): string {
  const v = s.value;
  if (v == null) return "";
  if (s.unit === "pct") return formatPercent(v, { digits: 1 });
  if (s.unit === "pct_points") return `${formatNumber(v, { digits: 0 })} %`;
  if (s.unit === "times") return `${formatNumber(v, { digits: 2 })}×`;
  return Math.abs(v) < 1 ? formatPercent(v, { digits: 1 }) : formatNumber(v, { digits: 2 });
}

/** Provider fields stored as fractions (0.219 = 21.9 %; ROE 1.46 = 146 %). */
const FRACTION_KEYS = new Set([
  "roe", "roic", "profit_margin", "gross_margin", "fcf_yield", "revenue_growth", "earnings_growth", "dividend_yield", "ter",
]);
/** Large amounts and share counts, shown compact. */
const AMOUNT_KEYS = new Set(["market_cap", "total_debt", "total_cash", "ebitda", "volume", "average_volume", "aum"]);

/** One raw fundamentals field in its unit; the grid printed ROE 1.46 and a 19.6 bn market cap with two decimals. */
const GROWTH_BASIS: Record<string, string> = {
  quarter_yoy: "latest quarter vs a year earlier",
  ttm_yoy: "last 12 months vs the 12 before",
};

export function formatFundamental(key: string, value: unknown): string {
  if (key === "growth_basis" && typeof value === "string") return GROWTH_BASIS[value] ?? value;
  if (typeof value !== "number") return String(value ?? "—");
  if (FRACTION_KEYS.has(key)) return formatPercent(value, { digits: 1 });
  if (key === "debt_equity") return `${formatNumber(value, { digits: 0 })} %`;
  if (key === "net_debt_ebitda") return `${formatNumber(value, { digits: 2 })}×`;
  if (AMOUNT_KEYS.has(key)) return formatCompact(value);
  return formatNumber(value, { digits: 2, minDigits: 0 });
}
