import { describe, expect, it } from "vitest";

import { expenseEditSchema, expenseSchema } from "./expenseSchema";

const expense = {
  date: "2026-05-22",
  amount: 12.34,
  currency: "EUR",
  description: "Groceries",
};

describe("expense schemas", () => {
  it("keeps new manual expenses positive", () => {
    expect(expenseSchema.safeParse({ ...expense, amount: -12.34 }).success).toBe(false);
  });

  it("allows negative imported amounts in the edit form", () => {
    expect(expenseEditSchema.safeParse({ ...expense, amount: -12.34 }).success).toBe(true);
  });
});
