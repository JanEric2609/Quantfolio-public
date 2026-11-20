import { describe, expect, it } from "vitest";
import { annualisedReturn, annualisedVolatility, arithmeticAnnual, dailyReturns, ols } from "./lessonMath";
import { isDue, recordAnswer } from "./quizSchedule";

describe("lesson maths", () => {
  it("compounds: +10 % then -10 % is -1 %, not 0", () => {
    expect(dailyReturns([100, 110, 99])[1]).toBeCloseTo(-0.1);
    expect(annualisedReturn([0.1, -0.1], 2)).toBeCloseTo(-0.01, 10);
  });
  it("the arithmetic shortcut overstates when returns vary", () => {
    const r = [0.1, -0.1, 0.1, -0.1];
    expect(arithmeticAnnual(r)).toBe(0);
    expect(annualisedReturn(r)).toBeLessThan(0);
  });
  it("matches the backend: volatility is sample std times sqrt(P)", () => {
    expect(annualisedVolatility([0.01, -0.01, 0.01, -0.01], 252)).toBeCloseTo(0.01154700538 * Math.sqrt(252), 6);
  });
  it("OLS recovers beta and alpha exactly on noise-free data", () => {
    const b = [0.01, -0.02, 0.03, 0.0, -0.01, 0.02];
    const p = b.map((x) => 0.001 + 1.5 * x);
    const fit = ols(p, b)!;
    expect(fit.beta).toBeCloseTo(1.5, 10);
    expect(fit.alpha).toBeCloseTo(0.001, 10);
    expect(fit.r2).toBeCloseTo(1, 10);
  });
  it("OLS refuses a flat benchmark and tiny samples", () => {
    expect(ols([0.1, 0.2, 0.3], [0, 0, 0])).toBeNull();
    expect(ols([0.1], [0.1])).toBeNull();
  });
});

describe("quiz schedule", () => {
  const DAY = 86_400_000;
  it("asks first, then at 2 days, 1 week and 3 weeks, then stops", () => {
    let s = {};
    expect(isDue(undefined, 0)).toBe(true);
    s = recordAnswer(s, "a", true, 0);
    expect(isDue(s["a" as keyof typeof s], DAY)).toBe(false);
    expect(isDue(s["a" as keyof typeof s], 2 * DAY)).toBe(true);
    s = recordAnswer(s, "a", true, 2 * DAY);
    expect(isDue(s["a" as keyof typeof s], 6 * DAY)).toBe(false);
    expect(isDue(s["a" as keyof typeof s], 7 * DAY)).toBe(true);
    s = recordAnswer(s, "a", false, 7 * DAY);
    expect(isDue(s["a" as keyof typeof s], 21 * DAY)).toBe(true);
    s = recordAnswer(s, "a", true, 21 * DAY);
    expect(isDue(s["a" as keyof typeof s], 400 * DAY)).toBe(false);
  });
});

import { bestOfK, bootstrapFan, eProcess, neweyWestT, rng, shrinkToDiagonal, walkForward } from "./lessonMath";

describe("lesson maths (WP14 second set)", () => {
  it("Newey-West with 0 lags equals the plain t and shrinks for positively autocorrelated data", () => {
    const g = rng(3);
    const x: number[] = [];
    let prev = 0;
    for (let i = 0; i < 300; i++) { prev = 0.7 * prev + (g() - 0.5); x.push(prev + 0.02); }
    expect(Math.abs(neweyWestT(x, 10))).toBeLessThan(Math.abs(neweyWestT(x, 0)));
  });
  it("the best of many null strategies looks significant", () => {
    expect(bestOfK(200, 250)).toBeGreaterThan(bestOfK(1, 250, 7) - 0.001);
    expect(bestOfK(500, 250)).toBeGreaterThan(2.5);
  });
  it("walk-forward out-of-sample edge is near zero for noise", () => {
    const g = rng(5);
    const x = Array.from({ length: 1000 }, () => g() - 0.5);
    const w = walkForward(x, 100, 50);
    expect(w.inSample).toBeGreaterThan(w.outSample);
  });
  it("e-process grows with a real edge and shrinks without one", () => {
    const g = rng(9);
    const edge = Array.from({ length: 400 }, () => 0.01 + 0.01 * (g() - 0.5));
    expect(eProcess(edge, 0.5).at(-1)!).toBeGreaterThan(20);
    const noise = Array.from({ length: 400 }, () => 0.01 * (g() - 0.5));
    expect(eProcess(noise, 0.5).at(-1)!).toBeLessThan(5);
  });
  it("shrinkage keeps the diagonal and scales the off-diagonal", () => {
    const s = shrinkToDiagonal([[1, 0.8], [0.8, 1]], 0.5);
    expect(s[0][0]).toBe(1);
    expect(s[0][1]).toBeCloseTo(0.4);
  });
  it("the bootstrap fan is ordered", () => {
    const g = rng(1);
    const f = bootstrapFan(Array.from({ length: 200 }, () => 0.0004 + 0.01 * (g() - 0.5)), 252);
    expect(f.p5).toBeLessThan(f.p50);
    expect(f.p50).toBeLessThan(f.p95);
  });
});
