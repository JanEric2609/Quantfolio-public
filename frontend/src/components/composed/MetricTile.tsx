import { cn } from "../../lib/utils";
import { formatNumber, formatPercent } from "../../lib/format";

/**
 * Shared replacement for the hand-rolled `fmt`/`pct`/`num` helpers that were
 * duplicated across AdvisorLoopTab.tsx, EvolutionTab.tsx, RiskView.tsx, and
 * OverviewView.tsx. `emptyText` defaults to an em-dash (the RiskView/
 * OverviewView convention); callers that want the advisor-loop's "pending"
 * wording (metric not yet computed, still maturing) pass it explicitly.
 */
export function formatMetricNumber(
  value: number | null | undefined,
  digits = 3,
  emptyText = "—",
): string {
  return value === null || value === undefined || !Number.isFinite(value) ? emptyText : formatNumber(value, { digits });
}

export function formatMetricPercent(
  value: number | null | undefined,
  digits = 2,
  emptyText = "—",
): string {
  return value === null || value === undefined || !Number.isFinite(value)
    ? emptyText
    : formatPercent(value, { digits });
}

export interface MetricTileProps {
  label: string;
  value: number | null | undefined;
  /** "number" -> `formatNumber(value, { digits })`; "percent" -> `formatPercent(value, { digits })` (value is a fraction). */
  format?: "number" | "percent";
  digits?: number;
  emptyText?: string;
  tone?: "neutral" | "good" | "warn" | "bad";
}

/**
 * Compact label/value row for a single metric — the layout unit used inside
 * a scorecard grid (e.g. the advisor loop's 4-axis scorecard, the evolution
 * champion/challenger cards). Not a full `Card` — see `KpiTile` for that.
 */
export function MetricTile({ label, value, format = "number", digits, emptyText, tone = "neutral" }: MetricTileProps) {
  const text =
    format === "percent"
      ? formatMetricPercent(value, digits ?? 2, emptyText)
      : formatMetricNumber(value, digits ?? 3, emptyText);
  const toneClass =
    tone === "good"
      ? "text-success"
      : tone === "bad"
        ? "text-danger"
        : tone === "warn"
          ? "text-warn"
          : "text-text-primary";
  return (
    <div className="flex items-baseline justify-between gap-2 text-xs">
      <span className="text-text-muted">{label}</span>
      <span className={cn("tabular-nums font-mono", toneClass)}>{text}</span>
    </div>
  );
}
