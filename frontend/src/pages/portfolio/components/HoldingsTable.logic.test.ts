import { describe, expect, it } from "vitest";
import { resolveLatestPrice } from "./HoldingsTable";

describe("resolveLatestPrice", () => {
  it("prefers the live quote price when available", () => {
    expect(resolveLatestPrice(190.5, "150.00")).toBe(190.5);
  });

  it("falls back to avg_buy_price when no live quote", () => {
    expect(resolveLatestPrice(undefined, "150.00")).toBe(150.0);
  });

  it("returns null when neither a quote nor a known avg_buy_price exists", () => {
    expect(resolveLatestPrice(undefined, null)).toBeNull();
  });
});
