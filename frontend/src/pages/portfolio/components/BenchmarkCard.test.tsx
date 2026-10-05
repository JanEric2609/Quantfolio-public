import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { LedgerBenchmark } from "../../../lib/api";
import { BenchmarkCard } from "./BenchmarkCard";

function bench(over: Partial<LedgerBenchmark> = {}): LedgerBenchmark {
  return {
    composite_id: "c1",
    benchmark_symbol: "EUNL.DE",
    period_start: "2026-01-02",
    period_end: "2026-09-30",
    period_days: 271,
    portfolio_twr: 0.043,
    benchmark_return: 0.071,
    excess: -0.028,
    short_window: true,
    message: null,
    ...over,
  };
}

describe("BenchmarkCard", () => {
  it("shows the TWR beside the benchmark over the same window, and flags a short one", () => {
    render(<BenchmarkCard benchmark={bench()} />);

    expect(screen.getByRole("heading", { name: /vs benchmark \(EUNL\.DE\)/ })).toBeInTheDocument();
    expect(screen.getByText("4.30 %")).toBeInTheDocument();
    expect(screen.getByText("7.10 %")).toBeInTheDocument();
    const diff = screen.getByText("−2.80 pp");
    expect(diff.className).toMatch(/text-danger/);
    expect(screen.getByText(/2026-01-02 to 2026-09-30 \(271 days\)/)).toHaveTextContent("Under a year");
  });

  it("is green when ahead and drops the short-window note after a year", () => {
    render(<BenchmarkCard benchmark={bench({ excess: 0.011, period_days: 400, short_window: false })} />);
    expect(screen.getByText("+1.10 pp").className).toMatch(/text-success/);
    expect(screen.queryByText(/Under a year/)).not.toBeInTheDocument();
  });

  it("shows the reason instead of a number when the benchmark has no price history", () => {
    render(<BenchmarkCard benchmark={bench({ benchmark_return: null, excess: null, message: "No EUNL.DE price history for this window yet." })} />);
    expect(screen.getByText("No EUNL.DE price history for this window yet.")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });
});
