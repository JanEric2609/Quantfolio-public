/** One list of chart ranges for every price or value chart (the stock page and the Dashboard, plan 2026-11-12 WP3). */
export const CHART_RANGES = [
  { key: "1W", label: "1W", days: 7 },
  { key: "1M", label: "1M", days: 31 },
  { key: "3M", label: "3M", days: 92 },
  { key: "6M", label: "6M", days: 183 },
  { key: "YTD", label: "YTD", days: 0 },
  { key: "1Y", label: "1Y", days: 366 },
  { key: "5Y", label: "5Y", days: 1830 },
  { key: "MAX", label: "Max", days: Infinity },
] as const;
export type ChartRangeKey = (typeof CHART_RANGES)[number]["key"];

/** The bars inside a range, counted back from the last bar (not from today, so a closed market still shows the range). */
export function sliceChartRange<T extends { date: string }>(rows: T[], key: ChartRangeKey): T[] {
  if (rows.length === 0) return rows;
  const days = CHART_RANGES.find((r) => r.key === key)?.days ?? 366;
  if (!Number.isFinite(days)) return rows;
  const lastDate = String(rows[rows.length - 1].date).slice(0, 10);
  const last = new Date(lastDate + "T00:00:00Z").getTime();
  // Year to date: from 1 January of the last bar's year.
  const from = key === "YTD" ? new Date(`${lastDate.slice(0, 4)}-01-01T00:00:00Z`).getTime() : last - days * 86_400_000;
  return rows.filter((r) => new Date(String(r.date).slice(0, 10) + "T00:00:00Z").getTime() >= from);
}

/** Calendar days a range asks the API for, counted back from ``today`` (YTD from 1 January; Max as far as the API allows). */
export function daysForRange(key: ChartRangeKey, today: Date = new Date()): number {
  if (key === "YTD") {
    const yearStart = Date.UTC(today.getUTCFullYear(), 0, 1);
    return Math.max(1, Math.floor((today.getTime() - yearStart) / 86_400_000) + 1);
  }
  const days = CHART_RANGES.find((r) => r.key === key)?.days ?? 366;
  return Number.isFinite(days) ? days : MAX_HISTORY_DAYS;
}

/** The longest history any chart requests (the API's day bound is far above it). */
export const MAX_HISTORY_DAYS = 3660;
