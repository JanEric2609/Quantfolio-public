import type { TrustCall, TrustState, TrustTypeVerdict } from "../../lib/api";

export type ChipTone = "neutral" | "good" | "warn" | "bad";

interface StateMeta {
  tone: ChipTone;
  /** Badge variant used by the chip. */
  badge: "secondary" | "success" | "warning" | "danger";
  /** Plain-language one-liner under the chip. */
  meaning: string;
}

export const STATE_META: Record<TrustState, StateMeta> = {
  too_early: {
    tone: "neutral",
    badge: "secondary",
    meaning: "Not enough data yet to say anything either way.",
  },
  skill: {
    tone: "good",
    badge: "success",
    meaning: "The record beats your ETF by more than luck explains.",
  },
  no_evidence: {
    tone: "warn",
    badge: "warning",
    meaning: "Enough data to look, and nothing that separates the record from luck.",
  },
  harm: {
    tone: "bad",
    badge: "danger",
    meaning: "The calls have done worse than simply holding your ETF, by more than luck explains.",
  },
};

/** The chip text: grey "Too early (N of ~needed)", green skill, amber no evidence, red harm. */
export function chipLabel(
  t: Pick<TrustTypeVerdict, "state" | "n" | "n_needed" | "benchmarked" | "type"> & { n_needed_is_lower_bound?: boolean },
): string {
  switch (t.state) {
    case "skill":
      return "Evidence of skill";
    case "harm":
      return t.benchmarked ? "Evidence of harm vs your ETF" : "Evidence of harm";
    case "no_evidence":
      return "No evidence yet";
    default:
      if (t.type === "regime") return "Not scored yet";
      return t.n_needed != null
        ? `Too early (${t.n} of ${fmtNeeded(t.n_needed, t.n_needed_is_lower_bound)})`
        : `Too early (${t.n})`;
  }
}

/** "~600", not "~617": the count needed is a rough planning number (matches the backend headline). */
export function aboutHundreds(n: number): number {
  return n >= 100 ? Math.round(n / 100) * 100 : n;
}

/** "~9,800" (or ">15,000" when the simulation only gave a lower bound): the dates needed, from the API. */
export function fmtNeeded(n: number, lowerBound?: boolean): string {
  return `${lowerBound ? ">" : "~"}${aboutHundreds(n).toLocaleString("en-US")}`;
}

/** "an edge of +1 % per 21 days with 5 % noise": the assumption behind the dates-needed number, from the API. */
export function assumptionText(v?: { assumed_edge?: number | null; assumed_sd?: number | null; horizon_days?: number | null }): string {
  if (v?.assumed_edge == null || v?.assumed_sd == null) return "a small true edge";
  const pct = (x: number) => `${Math.round(x * 1000) / 10}`;
  return `an edge of +${pct(v.assumed_edge)} % per ${v.horizon_days ?? 21} days with ${pct(v.assumed_sd)} % noise`;
}

/** "at one date per week, about 1,000 dates ≈ 21 years": dates needed at the observed cadence, from the API. */
export function cadenceText(v: {
  n_needed?: number | null;
  n_needed_is_lower_bound?: boolean;
  cadence_days?: number | null;
  years_needed?: number | null;
}): string | null {
  if (v.n_needed == null) return null;
  const gap = v.cadence_days ?? 1;
  const every = gap <= 1 ? "one date per trading day" : Math.abs(gap - 5) < 0.5 ? "one date per week" : `one date per ${gap} trading days`;
  const years = v.years_needed != null ? ` ≈ ${v.years_needed < 10 ? v.years_needed.toFixed(1) : Math.round(v.years_needed)} years` : "";
  return `at ${every}, ${v.n_needed_is_lower_bound ? "more than" : "about"} ${aboutHundreds(v.n_needed).toLocaleString("en-US")} dates${years}`;
}

export function fmtPct(value: number | null | undefined, digits = 0): string {
  return value == null || Number.isNaN(value) ? "—" : `${(value * 100).toFixed(digits)} %`;
}

/** A fraction as signed percentage points, e.g. 0.0123 -> "+1.2 pp". */
export function fmtPp(value: number | null | undefined, digits = 1): string {
  if (value == null || Number.isNaN(value)) return "—";
  const pp = value * 100;
  const sign = pp > 0 ? "+" : pp < 0 ? "−" : "±";
  return `${sign}${Math.abs(pp).toFixed(digits)} pp`;
}

export function fmtInterval(pair: [number, number] | null | undefined, kind: "pct" | "pp"): string | null {
  if (!pair) return null;
  const f = kind === "pct" ? (v: number) => fmtPct(v) : (v: number) => fmtPp(v);
  return `${f(pair[0])} to ${f(pair[1])}`;
}

export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString("en-GB", { year: "numeric", month: "short", day: "numeric" });
}

/** The earliest upcoming resolution across the given prediction types, if any. */
export function firstResolutionDue(types: Pick<TrustTypeVerdict, "next_resolution_at">[]): string | null {
  const stamps = types
    .map((t) => t.next_resolution_at)
    .filter((s): s is string => !!s)
    .sort();
  return stamps[0] ?? null;
}

export function outcomeText(call: TrustCall): string {
  if (call.verdict) return call.verdict;
  return call.outcome != null ? fmtPp(call.outcome) : "—";
}

/** "4.2" below ten years, "19" above: a planning number, not a measurement. */
export function fmtYears(years: number | null | undefined): string {
  if (years == null || Number.isNaN(years)) return "—";
  return years < 10 ? years.toFixed(1) : `${Math.round(years)}`;
}

/** "about 19 years (10–90 %: 6 to 42)" from the simulated time to know. */
export function timeToKnowText(t: {
  q10_years: number | null;
  q50_years: number | null;
  q90_years: number | null;
  max_days: number;
}): string {
  const cap = fmtYears(t.max_days / 252);
  if (t.q50_years == null) return `more than ${cap} years`;
  const lo = t.q10_years != null ? fmtYears(t.q10_years) : null;
  const hi = t.q90_years != null ? fmtYears(t.q90_years) : `more than ${cap}`;
  return `about ${fmtYears(t.q50_years)} years${lo ? ` (10–90 %: ${lo} to ${hi})` : ""}`;
}

/** "+1 % per 21 days" or "rank IC 0.07": the assumption behind a time-to-know number. */
export function assumptionLabel(unit: "excess_per_21d" | "rank_ic", value: number): string {
  return unit === "rank_ic" ? `rank IC ${value.toFixed(2)}` : `+${Math.round(value * 1000) / 10} % per 21 days`;
}

/** e-values span orders of magnitude: "0.42", "3.1", "1,250". */
export function fmtE(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  if (value >= 100) return Math.round(value).toLocaleString("en-US");
  return value >= 10 ? value.toFixed(1) : value.toFixed(2);
}
