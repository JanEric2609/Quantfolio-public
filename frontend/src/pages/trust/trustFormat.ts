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
    meaning: "Not enough independent rebalance dates yet to say anything either way.",
  },
  skill: {
    tone: "good",
    badge: "success",
    meaning: "The record is better than a coin flip by more than luck explains.",
  },
  no_evidence: {
    tone: "warn",
    badge: "warning",
    meaning: "Enough calls to look, and nothing that separates the record from a coin flip.",
  },
  harm: {
    tone: "bad",
    badge: "danger",
    meaning: "The calls have done worse than simply holding your ETF, by more than luck explains.",
  },
};

/** The chip text: grey "Too early (N of ~needed)", green skill, amber no evidence, red harm. */
export function chipLabel(t: Pick<TrustTypeVerdict, "state" | "n" | "n_needed" | "benchmarked" | "type">): string {
  switch (t.state) {
    case "skill":
      return "Evidence of skill";
    case "harm":
      return t.benchmarked ? "Evidence of harm vs your ETF" : "Evidence of harm";
    case "no_evidence":
      return "No evidence yet";
    default:
      if (t.type === "regime") return "Not scored yet";
      return t.n_needed != null ? `Too early (${t.n} of ~${aboutHundreds(t.n_needed)})` : `Too early (${t.n})`;
  }
}

/** "~600", not "~617": the count needed is a rough planning number (matches the backend headline). */
export function aboutHundreds(n: number): number {
  return n >= 100 ? Math.round(n / 100) * 100 : n;
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
