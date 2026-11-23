import { describe, expect, it } from "vitest";
import { calcPosition, countdown, floor4, type CalcFacts, type CalcInput } from "./cfdCalc";

const facts: CalcFacts = { marginRate: 0.05, overnightRate: 0.000083, fxFee: 0.005, minOrderEur: 20 };
const dax: CalcInput = { direction: "long", riskEur: 5, stopPoints: 250, level: 25000, eurPerPoint: 1, spreadBp: 0.5, currency: "EUR" };

describe("calcPosition", () => {
  it("sizes a EUR index from the risk and the stop", () => {
    const r = calcPosition(dax, facts)!;
    expect(r.quantity).toBe(0.02);
    expect(r.exposure).toBeCloseTo(500, 6);
    expect(r.margin).toBeCloseTo(25, 6);
    expect(r.spreadCost).toBeCloseTo(0.025, 6);
    expect(r.overnightPerNight).toBeCloseTo(0.0415, 6);
    expect(r.stopLevel).toBe(24750);
    expect(r.fxNote).toBeNull();
    expect(r.belowMinimum).toBe(false);
  });

  it("puts a short's stop above the level and rounds the quantity down", () => {
    const r = calcPosition({ ...dax, direction: "short", stopPoints: 437 }, facts)!;
    expect(r.stopLevel).toBe(25437);
    expect(r.quantity).toBe(0.0114); // 5 / 437 = 0.011441 -> 0.0114 (down)
    expect(floor4(0.0199999)).toBe(0.0199);
  });

  it("converts a USD index through EUR per USD and adds the FX note", () => {
    const r = calcPosition({ ...dax, level: 6800, stopPoints: 68, eurPerPoint: 0.9, currency: "USD" }, facts)!;
    expect(r.quantity).toBe(0.0816); // 5 / (68 * 0.9) = 0.08169...
    expect(r.exposure).toBeCloseTo(0.0816 * 6800 * 0.9, 6);
    expect(r.fxNote).toBe("USD market: 0.5 % FX fee on the result at close.");
  });

  it("warns below the 20 EUR minimum order", () => {
    expect(calcPosition({ ...dax, riskEur: 0.01 }, facts)!.belowMinimum).toBe(true);
  });

  it("gives nothing for unusable input", () => {
    expect(calcPosition({ ...dax, stopPoints: 0 }, facts)).toBeNull();
    expect(calcPosition({ ...dax, riskEur: Number.NaN }, facts)).toBeNull();
  });
});

describe("countdown", () => {
  it("words the time left", () => {
    const now = Date.parse("2026-11-24T10:00:00Z");
    expect(countdown("2026-11-24T13:12:00Z", now)).toBe("in 3 h 12 min");
    expect(countdown("2026-11-24T10:15:00Z", now)).toBe("in 15 min");
    expect(countdown("2026-11-24T09:00:00Z", now)).toBe("passed");
    expect(countdown(null, now)).toBe("-");
  });
});
