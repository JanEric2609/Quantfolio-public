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
