import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { LearningSleeveCard } from "./LearningSleeveCard";

const data = {
  purchases: [{ id: "p1", month: "2026-11-01", ticker: "AAA", name: "Aaa", amount_eur: 20, status: "filled", paper_price: 10,
    fill_quantity: 2, fill_price_eur: 10, fill_price_is_implied: true, fill_vs_paper_pct: 0 }],
  count_filled: 1, count_planned: 1, invested_eur: 20, real_value_eur: 24, shadow_value_eur: 21,
  note: "A learning expense you chose. It is not evidence that the picks work.",
};
let payload: unknown = data;
vi.mock("../../lib/api", () => ({
  getLearningSleeve: () => Promise.resolve(payload),
  setLearningFillPrice: () => Promise.resolve(payload),
}));

describe("LearningSleeveCard", () => {
  it("shows the expense wording, the count and both results", async () => {
    render(<LearningSleeveCard />);
    expect(await screen.findByText(/not evidence that the picks work/)).toBeTruthy();
    expect(screen.getByText(/1 of 1 planned purchases made/)).toBeTruthy();
  });
  it("renders nothing before the first planned purchase", async () => {
    payload = { ...data, purchases: [], count_planned: 0, count_filled: 0 };
    const { container } = render(<LearningSleeveCard />);
    await new Promise((r) => setTimeout(r, 20));
    expect(container.firstChild).toBeNull();
  });
});
