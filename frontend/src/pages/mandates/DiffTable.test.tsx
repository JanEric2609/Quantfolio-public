import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiffTable, buildRows } from "./DiffTable";

const onlyM = [{ isin: "A", ticker: "AAA", name: "A", quantity: "10", value_eur: 1000 }];
const onlyB = [{ isin: "D", ticker: "DDD", name: "D", quantity: "4", value_eur: 300 }];
const diffs = [
  { isin: "B", ticker: "BBB", paper_quantity: "5", real_quantity: "3", quantity_diff: 2, paper_value_eur: 1000, real_value_eur: 600 },
  { isin: "C", ticker: "CCC", paper_quantity: "2", real_quantity: "2", quantity_diff: 0, paper_value_eur: 100, real_value_eur: 100 },
];

describe("DiffTable", () => {
  it("lists differences by gap in euros, without matching positions", () => {
    expect(buildRows(onlyM, onlyB, diffs).map((r) => [r.ticker, r.gapEur])).toEqual([["AAA", 1000], ["BBB", 400], ["DDD", 300]]);
  });

  it("is collapsed by default and filters by kind", () => {
    render(<DiffTable onlyInMandate={onlyM} onlyInBook={onlyB} quantityDiffs={diffs} />);
    expect(screen.getByTestId("diff-details").hasAttribute("open")).toBe(false);
    expect(screen.getAllByTestId("diff-row")).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "Only in your book" }));
    const rows = screen.getAllByTestId("diff-row");
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain("DDD");
  });
});
