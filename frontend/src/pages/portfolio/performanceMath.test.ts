import { describe, expect, it } from "vitest";
import { drawdownSeries, monthlyReturns, periodReturn, sliceSeries, type Point } from "./performanceMath";

const pt = (date: string, unit_value: number, benchmark: number | null = null, net_flow = 0): Point => ({
  date,
  unit_value,
  value: unit_value * 1000,
  net_flow,
  benchmark,
});

const series = [
  pt("2025-12-30", 1.0, 100),
  pt("2025-12-31", 1.02, 101),
  pt("2026-01-15", 1.05, 103, 500),
  pt("2026-01-30", 1.1, 104),
  pt("2026-02-27", 0.99, 102, 200),
];

describe("performanceMath", () => {
  it("compounds the period return from first to last point", () => {
    expect(periodReturn([1, 1.1, 1.21])).toBeCloseTo(0.21);
    expect(periodReturn([1])).toBeNull();
    expect(periodReturn([null, 2, 3])).toBeCloseTo(0.5);
  });

  it("keeps the last day before the year as the base of year to date", () => {
    const ytd = sliceSeries(series, "ytd");
    expect(ytd[0].date).toBe("2025-12-31");
    expect(periodReturn(ytd.map((p) => p.unit_value))).toBeCloseTo(0.99 / 1.02 - 1);
    expect(sliceSeries(series, "all")).toHaveLength(5);
  });

  it("measures the fall from the running peak", () => {
    const dd = drawdownSeries(series);
    expect(dd[3][1]).toBe(0);
    expect(dd[4][1]).toBeCloseTo(0.99 / 1.1 - 1);
  });

  it("chains month-end unit values and sums each month's deposits", () => {
    const months = monthlyReturns(series);
    expect(months.map((m) => m.month)).toEqual(["2026-02", "2026-01", "2025-12"]);
    const jan = months[1];
    expect(jan.book).toBeCloseTo(1.1 / 1.02 - 1);
    expect(jan.benchmark).toBeCloseTo(104 / 101 - 1);
    expect(jan.netFlow).toBe(500);
    expect(months[2].partial).toBe(true);
    expect(months[0].netFlow).toBe(200);
  });
});
