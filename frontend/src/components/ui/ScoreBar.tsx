import { formatNumber } from "../../lib/format";

interface ScoreBarProps {
  label: string;
  value: number;
  colorClass?: string;
  /** Tooltip on the label, e.g. what the score is and is not. */
  hint?: string;
}

/**
 * A 0–1 score as a bar. Shown as "0.66", not "66 %": a ranking score is not a
 * probability, and a percentage reads like one.
 */
export function ScoreBar({ label, value, colorClass, hint }: ScoreBarProps) {
  const pct = Math.max(0, Math.min(100, value * 100));
  const barColor = colorClass ?? convictionBarColor(value);
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className="text-text-secondary" title={hint}>{label}</span>
        <span className="font-medium tabular-nums">{formatNumber(pct / 100, { digits: 2 })}</span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${barColor}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function convictionBarColor(conviction: number): string {
  if (conviction >= 0.7) return "bg-success";
  if (conviction >= 0.4) return "bg-warn";
  return "bg-danger";
}
