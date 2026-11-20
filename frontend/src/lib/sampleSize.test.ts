import { describe, expect, it } from "vitest";
import { annualisedSample, hitRateSample, icSample, MIN_RESOLVED_FOR_HIT_RATE } from "./sampleSize";

describe("sample-size guards", () => {
  it("withholds a hit rate below 30 resolved calls and states the interval above it", () => {
    expect(hitRateSample(0).enough).toBe(false);
    expect(hitRateSample(29).note).toContain("29 of 30");
    const ok = hitRateSample(MIN_RESOLVED_FOR_HIT_RATE);
    expect(ok.enough).toBe(true);
    expect(ok.note).toContain("±18 points");
    expect(hitRateSample(100).note).toContain("±10 points");
  });

  it("needs twelve dates for a mean IC", () => {
    expect(icSample(11).enough).toBe(false);
    expect(icSample(1).note).toContain("1 of 12");
    expect(icSample(12)).toEqual({ enough: true, note: "mean over 12 dates" });
  });

  it("also waits for independent months when the record overlaps, and uses the clustered interval", () => {
    const early = { windows: 3, min: 12, ciHalfWidth: null };
    expect(hitRateSample(60, early).enough).toBe(false);
    expect(hitRateSample(60, early).note).toContain("3 of 12 independent months");
    expect(icSample(14, early).enough).toBe(false);
    expect(icSample(14, early).note).toMatch(/^14 dates, but 3 of 12/);
    const ready = { windows: 12, min: 12, ciHalfWidth: 0.21 };
    expect(hitRateSample(180, ready)).toEqual({
      enough: true,
      note: "180 resolved over 12 independent months · 50 % is chance · ±21 points",
    });
    expect(icSample(48, ready).note).toBe("mean over 48 dates (12 independent months)");
  });

  it("needs a year for an annualised figure", () => {
    expect(annualisedSample(24).enough).toBe(false);
    expect(annualisedSample(1).note).toMatch(/^1 day of history/);
    const ok = annualisedSample(730);
    expect(ok.enough).toBe(true);
    expect(ok.note).toBe("2,0 years of history · ±0,7");
  });
});
