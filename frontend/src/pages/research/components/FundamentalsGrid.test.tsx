import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FundamentalsGrid } from "./FundamentalsGrid";

describe("FundamentalsGrid", () => {
  const base = { pe_ratio: 15, roe: 1.46, debt_equity: 590, pb_ratio: 20.9, name: "Wolters Kluwer N.V." };

  it("greys ROE and debt/equity when book equity is too small, and keeps other cells normal", () => {
    render(<FundamentalsGrid fundamentals={{ ...base, book_ratios_unreliable: true }} />);
    expect(screen.getByTestId("fund-roe").getAttribute("data-greyed")).toBe("true");
    expect(screen.getByTestId("fund-debt_equity").getAttribute("data-greyed")).toBe("true");
    expect(screen.getByTestId("fund-pe_ratio").getAttribute("data-greyed")).toBeNull();
    expect(screen.getAllByText(/book equity too small/).length).toBe(2);
  });

  it("shows every cell normally for an ordinary balance sheet, in a fixed order with fixed labels", () => {
    const { container } = render(<FundamentalsGrid fundamentals={{ ...base, book_ratios_unreliable: false }} />);
    expect(screen.getByTestId("fund-roe").getAttribute("data-greyed")).toBeNull();
    const labels = Array.from(container.querySelectorAll("div.text-\\[11px\\]")).map((e) => e.textContent);
    expect(labels).toEqual(["P/E (trailing)", "Price / book", "Return on equity", "Debt / equity"]);
    expect(screen.queryByText("name")).toBeNull();
  });
});

describe("FundamentalsGrid reporting period", () => {
  it("names the period the growth figures end on", () => {
    render(<FundamentalsGrid fundamentals={{ revenue_growth: -0.006, growth_basis: "quarter_yoy", latest_period_end: "2026-06-30" }} />);
    expect(screen.getByTestId("fund-period-note").textContent).toContain("2026-06-30");
    expect(screen.queryByTestId("fund-latest_period_end")).toBeNull();
  });
});

describe("FundamentalsGrid WP2", () => {
  it("shows n/a with the reason when the free-cash-flow yield cannot be computed", () => {
    render(<FundamentalsGrid fundamentals={{ pe_ratio: 12, fcf_yield: null, fcf_yield_reason: "currency_mismatch" }} />);
    expect(screen.getByTestId("fund-fcf_yield").textContent).toContain("different currencies");
  });
  it("names a half-year and puts the currency beside the market cap", () => {
    render(<FundamentalsGrid fundamentals={{ market_cap: 1.9e10, currency: "EUR", revenue_growth: 0.02, growth_basis: "quarter_yoy", latest_period_end: "2026-06-30", period_months: 6 }} />);
    expect(screen.getByTestId("fund-period-note").textContent).toContain("half-year to 2026-06-30");
    expect(screen.getByTestId("fund-market_cap").textContent).toContain("EUR");
  });
  it("hides unknown raw keys", () => {
    render(<FundamentalsGrid fundamentals={{ pe_ratio: 12, some_new_key: 5 }} />);
    expect(screen.queryByTestId("fund-some_new_key")).toBeNull();
  });
});
