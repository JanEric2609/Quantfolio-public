import { describe, expect, it } from "vitest";
import { sliceChartRange } from "./chartRange";

const rows = Array.from({ length: 400 }, (_, i) => ({
  date: new Date(Date.UTC(2025, 0, 1) + i * 86_400_000).toISOString().slice(0, 10),
}));

describe("sliceChartRange", () => {
  it("counts back from the last bar", () => {
    expect(sliceChartRange(rows, "1W").length).toBe(8);
    expect(sliceChartRange(rows, "1M").length).toBe(32);
    expect(sliceChartRange(rows, "5Y").length).toBe(400);
  });
  it("handles no data", () => {
    expect(sliceChartRange([], "1M")).toEqual([]);
  });
});

describe("YTD and Max", () => {
  const rows = [
    { date: "2025-12-30" }, { date: "2026-01-02" }, { date: "2026-06-01" }, { date: "2026-11-10" },
  ];
  it("YTD starts on 1 January of the last bar's year", () => {
    expect(sliceChartRange(rows, "YTD").map((r) => r.date)).toEqual(["2026-01-02", "2026-06-01", "2026-11-10"]);
  });
  it("Max keeps every bar", () => {
    expect(sliceChartRange(rows, "MAX").length).toBe(4);
  });
});
