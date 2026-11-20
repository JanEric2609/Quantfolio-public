import { describe, expect, it } from "vitest";
import { formatFundamental, formatSignalValue } from "./strengthScore";

describe("formatSignalValue", () => {
  it("shows a fraction above 1 as a percentage (Wolters Kluwer ROE 1.46)", () => {
    expect(formatSignalValue({ value: 1.46141, unit: "pct" })).toContain("146");
  });
  it("shows debt/equity points as percent and multiples with a sign", () => {
    expect(formatSignalValue({ value: 590.213, unit: "pct_points" })).toBe("590 %");
    expect(formatSignalValue({ value: 2.08, unit: "times" })).toMatch(/2[.,]08×/);
  });
  it("keeps the old reading for payloads without a unit", () => {
    expect(formatSignalValue({ value: 15.06 })).toMatch(/15[.,]06/);
  });
});

describe("formatFundamental", () => {
  it("formats each field in its unit", () => {
    expect(formatFundamental("roe", 1.46141)).toContain("146");
    expect(formatFundamental("gross_margin", 0.74075)).toContain("74");
    expect(formatFundamental("debt_equity", 590.213)).toBe("590 %");
    expect(formatFundamental("pe_ratio", 15.063249)).toMatch(/15[.,]06/);
    expect(formatFundamental("sector", "Industrials")).toBe("Industrials");
    expect(formatFundamental("growth_basis", "quarter_yoy")).toBe("latest quarter vs a year earlier");
  });
});
