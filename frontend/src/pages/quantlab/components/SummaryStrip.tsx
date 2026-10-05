import type { ReactNode } from "react";
import { MetricTooltip } from "../../../components/composed/MetricTooltip";

export interface MetricItem {
  label: string;
  value: string;
  mono?: boolean;
  delta?: string;
  deltaPositive?: boolean;
  metricKey?: string;
  metricValue?: number | null;
  /** Neutral footnote under the value (e.g. why it is withheld). */
  hint?: string;
}

interface SummaryStripProps {
  metrics: MetricItem[];
  sparkline?: ReactNode;
}

export function SummaryStrip({ metrics, sparkline }: SummaryStripProps) {
  return (
    <div
      className="flex items-stretch gap-0 overflow-x-auto border-b border-line"
      style={{ height: 160, minHeight: 160 }}
    >
      {metrics.map((metric, i) => (
        <div
          key={metric.label}
          className="flex flex-col justify-center px-5"
          style={{
            borderRight: i < metrics.length - 1 ? "1px solid rgb(var(--c-border))" : undefined,
            minWidth: 120,
          }}
        >
          <span
            className="text-[11px] uppercase flex items-center gap-1"
            style={{
              fontFamily: '"Inter Tight", ui-sans-serif, system-ui, sans-serif',
              letterSpacing: "0.06em",
              color: "rgb(var(--c-text-muted))",
            }}
          >
            {metric.label}
            {metric.metricKey && (
              <MetricTooltip metricKey={metric.metricKey} value={metric.metricValue}>
                <span className="inline-flex cursor-help text-text-muted">
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                </span>
              </MetricTooltip>
            )}
          </span>
          <span
            className="mt-1"
            style={{
              fontFamily: metric.mono !== false ? '"JetBrains Mono", ui-monospace, monospace' : undefined,
              fontVariantNumeric: "tabular-nums",
              fontWeight: 600,
              fontSize: 28,
              lineHeight: 1.1,
              color: "rgb(var(--c-text-primary))",
            }}
          >
            {metric.value}
          </span>
          {metric.delta && (
            <span
              className="mt-0.5 text-[11px]"
              style={{
                color: metric.deltaPositive
                  ? "rgb(var(--c-success))"
                  : "rgb(var(--c-danger))",
                fontFamily: '"JetBrains Mono", ui-monospace, monospace',
              }}
            >
              {metric.delta}
            </span>
          )}
          {metric.hint && (
            <span className="mt-0.5 text-[11px] text-text-muted">{metric.hint}</span>
          )}
        </div>
      ))}
      {sparkline && (
        <div className="ml-auto flex items-center px-3 opacity-60">{sparkline}</div>
      )}
    </div>
  );
}
