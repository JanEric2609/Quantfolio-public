import type { BookPerformance } from "../../lib/api";

export type Point = BookPerformance["series"][number];
export type Period = "all" | "ytd" | "1y";

/** The series from the period's first day on (the whole series for "all"). */
export function sliceSeries(series: Point[], period: Period): Point[] {
  if (period === "all" || series.length === 0) return series;
  const end = series[series.length - 1].date;
  const cut =
    period === "ytd"
      ? `${end.slice(0, 4)}-01-01`
      : (() => {
          const d = new Date(`${end}T00:00:00Z`);
          d.setUTCFullYear(d.getUTCFullYear() - 1);
          return d.toISOString().slice(0, 10);
        })();
  // Keep the last day before the cut as the base, so the period's first
  // day's return counts.
  const firstIn = series.findIndex((p) => p.date >= cut);
  if (firstIn < 0) return [];
  return series.slice(Math.max(firstIn - 1, 0));
}

/** Growth from the first to the last point, or null with fewer than two. */
export function periodReturn(values: Array<number | null>): number | null {
  const v = values.filter((x): x is number => x != null && Number.isFinite(x) && x > 0);
  if (v.length < 2) return null;
  return v[v.length - 1] / v[0] - 1;
}

/** Drawdown of the unit value from its running peak, per day. */
export function drawdownSeries(series: Point[]): Array<[string, number]> {
  let peak = 0;
  return series.map((p) => {
    peak = Math.max(peak, p.unit_value);
    return [p.date, peak > 0 ? p.unit_value / peak - 1 : 0];
  });
}

export interface MonthRow {
  month: string; // YYYY-MM
  book: number | null;
  benchmark: number | null;
  netFlow: number;
  partial: boolean;
}

/**
 * Month-by-month time-weighted return of the book and the benchmark: the
 * month's last unit value over the previous month's last (the first month
 * starts at the series' first day and is marked partial, as is a running month).
 */
export function monthlyReturns(series: Point[]): MonthRow[] {
  if (series.length < 2) return [];
  const rows: MonthRow[] = [];
  let base = series[0];
  let flow = 0;
  for (let i = 1; i < series.length; i += 1) {
    const p = series[i];
    flow += p.net_flow;
    const next = series[i + 1];
    const monthEnds = !next || next.date.slice(0, 7) !== p.date.slice(0, 7);
    if (!monthEnds) continue;
    rows.push({
      month: p.date.slice(0, 7),
      book: base.unit_value > 0 ? p.unit_value / base.unit_value - 1 : null,
      benchmark: base.benchmark != null && p.benchmark != null && base.benchmark > 0 ? p.benchmark / base.benchmark - 1 : null,
      netFlow: flow,
      partial: rows.length === 0 && base.date.slice(8, 10) !== "01" ? true : !next,
    });
    base = p;
    flow = 0;
  }
  return rows.reverse();
}
