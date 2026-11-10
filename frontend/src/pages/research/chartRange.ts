export const CHART_RANGES = [
  { key: "1W", label: "1W", days: 7 },
  { key: "1M", label: "1M", days: 31 },
  { key: "3M", label: "3M", days: 92 },
  { key: "6M", label: "6M", days: 183 },
  { key: "1Y", label: "1Y", days: 366 },
  { key: "5Y", label: "5Y", days: 1830 },
] as const;
export type ChartRangeKey = (typeof CHART_RANGES)[number]["key"];

/** The bars inside a range, counted back from the last bar (not from today, so a closed market still shows the range). */
export function sliceChartRange<T extends { date: string }>(rows: T[], key: ChartRangeKey): T[] {
  if (rows.length === 0) return rows;
  const days = CHART_RANGES.find((r) => r.key === key)?.days ?? 366;
  const last = new Date(String(rows[rows.length - 1].date).slice(0, 10) + "T00:00:00Z").getTime();
  const from = last - days * 86_400_000;
  return rows.filter((r) => new Date(String(r.date).slice(0, 10) + "T00:00:00Z").getTime() >= from);
}
