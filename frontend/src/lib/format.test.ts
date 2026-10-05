import { describe, expect, it, vi } from "vitest";
import {
  formatCompact,
  formatCurrency,
  formatDate,
  formatDateTime,
  formatMonth,
  formatNumber,
  formatPercent,
  formatPercentPoints,
  formatSignedDelta,
  setReportingCurrency,
} from "./format";

// Intl separates the number from "€" / "%" with a no-break space.
const NBSP = " ";

describe("format helpers (fixed de-DE app locale)", () => {
  it("formatNumber uses German separators and 2 digits by default", () => {
    expect(formatNumber(1234.5)).toBe("1.234,50");
    expect(formatNumber(0.5)).toBe("0,50");
    expect(formatNumber(1234.5678, { digits: 0 })).toBe("1.235");
    expect(formatNumber(1234.5678, { decimals: 3 })).toBe("1.234,568");
    expect(formatNumber(12.3, { digits: 4, minDigits: 0 })).toBe("12,3");
    expect(formatNumber(2.5, { signed: true })).toBe("+2,50");
    expect(formatNumber(0, { signed: true })).toBe("0,00");
  });

  it("renders a dash for null, undefined, NaN and Infinity", () => {
    expect(formatNumber(null)).toBe("—");
    expect(formatNumber(undefined)).toBe("—");
    expect(formatNumber(Number.NaN)).toBe("—");
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(Number.POSITIVE_INFINITY)).toBe("—");
    expect(formatCurrency(undefined)).toBe("—");
    expect(formatCurrency(Number.NaN)).toBe("—");
    expect(formatPercentPoints(null)).toBe("—");
    expect(formatCompact(null)).toBe("—");
    expect(formatDate(null)).toBe("—");
    expect(formatDate("not a date")).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
  });

  it("formatPercent takes a fraction and formats it with Intl", () => {
    expect(formatPercent(0.1234)).toBe(`12,34${NBSP}%`);
    expect(formatPercent(0.123, { digits: 1 })).toBe(`12,3${NBSP}%`);
    expect(formatPercent(0.123, { decimals: 0 })).toBe(`12${NBSP}%`);
    expect(formatPercent(0.012, { digits: 1, signed: true })).toBe(`+1,2${NBSP}%`);
    expect(formatPercent(-0.012, { digits: 1, signed: true })).toBe(`-1,2${NBSP}%`);
    expect(formatPercent(0, { digits: 1, signed: true })).toBe(`0,0${NBSP}%`);
  });

  it("formatPercentPoints takes values that are already in percent units", () => {
    expect(formatPercentPoints(12.3, { digits: 1 })).toBe(`12,3${NBSP}%`);
    expect(formatPercentPoints(-0.5, { digits: 1, signed: true })).toBe(`-0,5${NBSP}%`);
  });

  it("formatCurrency defaults to EUR and accepts the currency positionally or in options", () => {
    expect(formatCurrency(1234.56)).toBe(`1.234,56${NBSP}€`);
    expect(formatCurrency(1234.56, "EUR")).toBe(`1.234,56${NBSP}€`);
    expect(formatCurrency(1234.56, { currency: "EUR" })).toBe(`1.234,56${NBSP}€`);
    expect(formatCurrency(1234.56, "USD")).toBe(`1.234,56${NBSP}$`);
    expect(formatCurrency(1234.56, { digits: 0 })).toBe(`1.235${NBSP}€`);
  });

  it("formatCurrency supports signed and compact", () => {
    expect(formatCurrency(12, "EUR", { signed: true })).toBe(`+12,00${NBSP}€`);
    expect(formatCurrency(-12, "EUR", { signed: true })).toBe(`-12,00${NBSP}€`);
    expect(formatCurrency(1_234_567, "EUR", { compact: true })).toMatch(/^1,2\s*Mio\.\s*€$/);
    // German CLDR has no compact form below a million: thousands stay spelled out.
    expect(formatCurrency(12_500, { compact: true })).toBe(`12.500${NBSP}€`);
  });

  it("formatCurrency follows the reporting currency when none is given", () => {
    setReportingCurrency("CHF");
    expect(formatCurrency(5)).toMatch(/CHF/);
    setReportingCurrency("EUR");
    expect(formatCurrency(5)).toBe(`5,00${NBSP}€`);
  });

  it("formatCompact uses German compact notation", () => {
    expect(formatCompact(1_500_000)).toMatch(/^1,5\s*Mio\.$/);
  });

  it("formatDate / formatDateTime use German day.month.year order", () => {
    expect(formatDate("2026-09-30")).toBe("30.09.2026");
    expect(formatDate("2026-09-30", { style: "short" })).toBe("30.09.26");
    expect(formatDate(new Date(2026, 0, 5))).toBe("05.01.2026");
    expect(formatDateTime(new Date(2026, 8, 30, 14, 5))).toMatch(/^30\.09\.2026,? 14:05$/);
  });

  it("formatDate keeps a date-only string on its own calendar day", () => {
    // Would be 29.09. in any UTC-negative timezone if parsed as UTC midnight.
    expect(formatDate("2026-09-30")).toBe("30.09.2026");
    expect(formatDate("2026-01-01")).toBe("01.01.2026");
  });

  it("formatMonth names the month in German", () => {
    expect(formatMonth("2026-10")).toBe("Oktober 2026");
    expect(formatMonth("2026-03-15")).toBe("März 2026");
    expect(formatMonth(new Date(2026, 0, 1))).toBe("Januar 2026");
    expect(formatMonth("garbage")).toBe("garbage");
    expect(formatMonth(null)).toBe("—");
  });

  it("formatSignedDelta number kind", () => {
    expect(formatSignedDelta(5, "number")).toMatchObject({ text: "+5,00", tone: "up", arrow: "▲" });
    expect(formatSignedDelta(-2.5, "number")).toMatchObject({ text: "-2,50", tone: "down", arrow: "▼" });
    expect(formatSignedDelta(0, "number")).toMatchObject({ text: "±0,00", tone: "flat" });
    expect(formatSignedDelta(null)).toMatchObject({ text: "—", tone: "flat" });
  });

  it("formatSignedDelta percent and currency kinds", () => {
    expect(formatSignedDelta(0.052, "percent")).toMatchObject({ text: `+5,20${NBSP}%`, tone: "up" });
    expect(formatSignedDelta(-0.052, "percent", { digits: 1 })).toMatchObject({ text: `-5,2${NBSP}%`, tone: "down" });
    expect(formatSignedDelta(12.5, "currency", { currency: "EUR" })).toMatchObject({ text: `+12,50${NBSP}€`, tone: "up" });
  });

  it("formatSignedDelta carries a screen-reader label so colour is never the only cue", () => {
    expect(formatSignedDelta(0.052, "percent").label).toBe(`up +5,20${NBSP}%`);
    expect(formatSignedDelta(-3, "number").label).toBe("down -3,00");
    expect(formatSignedDelta(0, "number").label).toBe("unchanged ±0,00");
  });
});

describe("locale handling", () => {
  it("ignores the browser language entirely", async () => {
    vi.resetModules();
    const spy = vi.spyOn(navigator, "language", "get").mockReturnValue("en-US");
    const fresh = await import("./format");
    expect(fresh.formatNumber(1234.5)).toBe("1.234,50");
    spy.mockRestore();
  });

  it("survives a browser language that is not a valid BCP 47 tag", async () => {
    // Headless/POSIX environments report e.g. "en-US@posix"; Intl throws on it.
    vi.resetModules();
    const spy = vi.spyOn(navigator, "language", "get").mockReturnValue("en-US@posix");
    const fresh = await import("./format");
    expect(() => fresh.formatCurrency(1234.5, { currency: "EUR" })).not.toThrow();
    spy.mockRestore();
  });

  it("falls back to the app locale for an invalid locale override", () => {
    expect(formatNumber(1234.5, { locale: "en-US@posix" })).toBe("1.234,50");
    expect(formatNumber(1234.5, { locale: "en-GB" })).toBe("1,234.50");
  });

  it("does not throw on an unknown currency code", () => {
    expect(() => formatCurrency(5, "NOPE1")).not.toThrow();
  });
});
