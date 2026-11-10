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
