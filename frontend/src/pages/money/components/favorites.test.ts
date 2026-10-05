import { describe, expect, it } from "vitest";
import { deriveFavorites } from "./favorites";

const NOW = new Date("2026-07-24T12:00:00Z");

describe("deriveFavorites", () => {
  it("surfaces expenses that recur at least twice within the window", () => {
    const expenses = [
      { date: "2026-07-01", description: "Coffee", amount: "-3.50", category_id: "cat-food" },
      { date: "2026-07-10", description: "Coffee", amount: "-3.50", category_id: "cat-food" },
      { date: "2026-07-15", description: "One-off gadget", amount: "-89.00", category_id: "cat-misc" },
    ];

    const favorites = deriveFavorites(expenses, { now: NOW });

    expect(favorites).toHaveLength(1);
    expect(favorites[0]).toMatchObject({
      description: "Coffee",
      amount: 3.5,
      category_id: "cat-food",
      count: 2,
    });
  });

  it("surfaces recurring positive-amount expenses (the shape QuickAddExpense produces)", () => {
    const expenses = [
      { date: "2026-07-01", description: "Coffee", amount: "3.50", category_id: "cat-food" },
      { date: "2026-07-10", description: "Coffee", amount: "3.50", category_id: "cat-food" },
    ];

    const favorites = deriveFavorites(expenses, { now: NOW });

    expect(favorites).toHaveLength(1);
    expect(favorites[0]).toMatchObject({ description: "Coffee", amount: 3.5, category_id: "cat-food", count: 2 });
  });

  it("ignores expenses outside the lookback window", () => {
    const expenses = [
      { date: "2026-01-01", description: "Old thing", amount: "-10", category_id: "cat-misc" },
      { date: "2026-01-08", description: "Old thing", amount: "-10", category_id: "cat-misc" },
    ];

    expect(deriveFavorites(expenses, { now: NOW, windowDays: 60 })).toHaveLength(0);
  });

  it("sorts by frequency and respects the limit", () => {
    const expenses = [
      ...Array.from({ length: 3 }, (_, i) => ({ date: `2026-07-0${i + 1}`, description: "Coffee", amount: "-3.5", category_id: "cat-food" })),
      ...Array.from({ length: 5 }, (_, i) => ({ date: `2026-07-1${i}`, description: "Snacks", amount: "-2", category_id: "cat-food" })),
    ];

    const favorites = deriveFavorites(expenses, { now: NOW, limit: 1 });

    expect(favorites).toHaveLength(1);
    expect(favorites[0].description).toBe("Snacks");
  });
});
