import { type ReactNode } from "react";
import { Info } from "lucide-react";
import { TapTooltip } from "./TapTooltip";
import {
  METRIC_META,
  getMetricSeverity,
  formatMetricValue,
} from "../../lib/metricMeta";

const SEVERITY_COLORS: Record<string, string> = {
  good: "text-success",
  warn: "text-warn",
  bad: "text-danger",
  neutral: "text-text-muted",
};

interface MetricTooltipProps {
  metricKey: string;
  value?: number | null;
  children?: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
}

export function MetricTooltip({
  metricKey,
  value,
  children,
  side = "top",
}: MetricTooltipProps) {
  const meta = METRIC_META[metricKey];
  if (!meta) return <>{children ?? <Info size={12} className="text-text-muted" aria-hidden />}</>;

  const severity = getMetricSeverity(meta, value);
  const formattedValue = formatMetricValue(metricKey, value);
  const severityColor = SEVERITY_COLORS[severity];

  const unitLabel =
    meta.unit === "percent"
      ? "Percentage"
      : meta.unit === "ratio"
        ? "Ratio"
        : meta.unit === "days"
          ? "Days"
          : meta.unit === "count"
            ? "Count"
            : meta.unit === "currency"
              ? "Currency"
              : "Decimal";

  return (
    <TapTooltip
      side={side}
      ariaLabel={`About ${meta.label}`}
      hitArea
      className="inline-flex text-text-muted"
      contentClassName="p-3"
      content={
      <div className="space-y-1.5">
        {/* Header: metric name */}
        <p className="font-semibold text-text-primary text-sm">{meta.label}</p>

        {/* Current value with severity badge */}
        {value != null && !isNaN(value) && (
          <div className="flex items-center gap-2">
            <span className={`font-mono tabular-nums text-sm font-medium ${severityColor}`}>
              {formattedValue}
            </span>
            {severity !== "neutral" && (
              <span
                className={`text-[10px] uppercase tracking-wider font-medium px-1.5 py-0.5 rounded ${
                  severity === "good"
                    ? "bg-success/15 text-success"
                    : severity === "warn"
                      ? "bg-warn/15 text-warn"
                      : "bg-danger/15 text-danger"
                }`}
              >
                {severity}
              </span>
            )}
          </div>
        )}

        {/* Description */}
        <p className="text-xs text-text-secondary leading-relaxed">
          {meta.description}
        </p>

        {/* Unit + severity reference */}
        <div className="flex items-center gap-2 text-[11px] text-text-muted pt-0.5 border-t border-border/50">
          <span>{unitLabel}</span>
          {meta.severity && (
            <>
              <span className="text-border">|</span>
              <span>
                {meta.severity.invert ? "Lower is better" : "Higher is better"}
              </span>
            </>
          )}
        </div>
      </div>
      }
    >
      {children ?? <Info size={12} aria-hidden />}
    </TapTooltip>
  );
}
