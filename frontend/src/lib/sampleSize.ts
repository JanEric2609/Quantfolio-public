/**
 * Sample-size guards (report Phase 5). A statistic from too few observations
 * looks like information and is noise, so below these minimums the UI shows
 * how far along the sample is instead of the number.
 *
 * - Hit rate: at 30 resolved calls the 95 % interval around a true 50 % is
 *   still about +/-18 points; below that a streak of luck reads as skill.
 * - Mean rank IC: one IC per scoring date; a year of monthly dates (12)
 *   before its t-statistic means anything.
 * - Overlapping windows: a prediction is judged over about a month, so
 *   weekly (or daily) scoring dates share most of their market window and
 *   are not separate evidence. Where the backend reports it, the hit rate and
 *   the IC also wait for 12 independent (non-overlapping) months, and the
 *   interval is the backend's, clustered by date.
 * - Annualised Sharpe: a year of history. Its standard error is about
 *   1/sqrt(years), so after a month any figure between -3.5 and +3.5 is noise.
 */
import { formatNumber } from "./format";

export const MIN_RESOLVED_FOR_HIT_RATE = 30;
export const MIN_DATES_FOR_IC = 12;
export const MIN_DAYS_FOR_ANNUALISED = 365;

/** Non-overlapping horizons in the record, from the backend. */
export interface WindowInfo {
  windows: number;
  min: number;
  /** 95 % half-width clustered by date, as a fraction; null when not measurable. */
  ciHalfWidth?: number | null;
}

function windowsShort(win: WindowInfo): string {
  return `${win.windows} of ${win.min} independent months; nearby dates share one market window`;
}

export interface SampleCheck {
  enough: boolean;
  /** One line for under the number: the sample, and what it means. */
  note: string;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export function hitRateSample(resolved: number, win?: WindowInfo): SampleCheck {
  if (resolved < MIN_RESOLVED_FOR_HIT_RATE) {
    return {
      enough: false,
      note: `${resolved} of ${MIN_RESOLVED_FOR_HIT_RATE} resolved calls needed; too few to tell skill from luck`,
    };
  }
  if (win && win.windows < win.min) {
    return { enough: false, note: `${resolved} resolved, but ${windowsShort(win)}` };
  }
  const half = win?.ciHalfWidth != null ? win.ciHalfWidth : Math.sqrt(0.25 / resolved) * 1.96;
  const over = win ? ` over ${win.windows} independent months` : "";
  return { enough: true, note: `${resolved} resolved${over} · 50 % is chance · ±${Math.round(100 * half)} points` };
}

export function icSample(dates: number, win?: WindowInfo): SampleCheck {
  if (dates < MIN_DATES_FOR_IC) {
    return {
      enough: false,
      note: `${dates} of ${MIN_DATES_FOR_IC} scoring dates needed before the average means anything`,
    };
  }
  if (win && win.windows < win.min) {
    return { enough: false, note: `${plural(dates, "date")}, but ${windowsShort(win)}` };
  }
  return {
    enough: true,
    note: `mean over ${plural(dates, "date")}${win ? ` (${win.windows} independent months)` : ""}`,
  };
}

export function annualisedSample(days: number): SampleCheck {
  if (days >= MIN_DAYS_FOR_ANNUALISED) {
    const years = days / 365;
    return { enough: true, note: `${formatNumber(years, { digits: 1 })} years of history · ±${formatNumber(1 / Math.sqrt(years), { digits: 1 })}` };
  }
  return {
    enough: false,
    note: `${plural(days, "day")} of history; an annualised figure needs a year before it is more than noise`,
  };
}

/** The label shown in place of a number whose sample is too small. */
export const TOO_EARLY = "Too early";
