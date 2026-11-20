import type { ReactNode } from "react";
import { HelpCircle } from "lucide-react";
import { Card } from "../ui/card";
import { cn } from "../../lib/utils";
import { formatSignedDelta } from "../../lib/format";
import { DeltaText } from "./DeltaText";
import { GlossaryTooltip } from "./GlossaryTooltip";
import { MetricTooltip } from "./MetricTooltip";
import { METRIC_META, getMetricSeverity } from "../../lib/metricMeta";
import { Sparkline } from "../charts/Sparkline";

export interface KpiTileProps {
  label: string;
  value: ReactNode;
  /** Signed delta value. Interpreted by `deltaFormat`. */
  delta?: number | null;
  /** How to format `delta`. Default: "percent" (0.052 -> "+5,20 %"). */
  deltaFormat?: "number" | "percent" | "currency";
  /** Backwards-compatible alias used by existing pages. */
  deltaKind?: "number" | "percent" | "currency";
  /** ISO 4217 code, only used when `deltaFormat === "currency"`. Default "EUR". */
  deltaCurrency?: string;
  /** Visual tone for the value text. Default "neutral". */
  tone?: "neutral" | "good" | "warn" | "bad";
  /** Key into `glossary.ts`. If present, label gets a "(?)" tooltip trigger. */
  glossaryKey?: string;
  /** Key into `metricMeta.ts`. When provided, uses MetricTooltip and auto-derives tone. */
  metricKey?: string;
  /** Numeric value for metric severity lookup. Used with `metricKey`. */
  metricValue?: number | null;
  /** Optional sparkline values; height 32. */
  sparkline?: number[];
  /** Render value with `font-mono tabular-nums`. Default true. */
  mono?: boolean;
}

export function KpiTile({
  label,
  value,
  delta,
  deltaFormat,
  deltaKind,
  deltaCurrency,
  tone = "neutral",
  glossaryKey,
  metricKey,
  metricValue,
  sparkline,
  mono = true,
}: KpiTileProps) {
  const deltaInfo = delta != null ? formatSignedDelta(delta, deltaFormat ?? deltaKind ?? "percent", { currency: deltaCurrency }) : null;

  const autoSeverity =
    metricKey && tone === "neutral"
      ? getMetricSeverity(METRIC_META[metricKey] ?? { label: "", unit: "ratio", decimals: 2, description: "" }, metricValue)
      : null;

  const effectiveTone = autoSeverity ?? tone;

  const resolvedGlossaryKey = metricKey ? METRIC_META[metricKey]?.glossaryKey ?? glossaryKey : glossaryKey;

  const valueTone = effectiveTone === "good" ? "text-success" : effectiveTone === "bad" ? "text-danger" : effectiveTone === "warn" ? "text-warn" : "text-text-primary";

  return (
    <Card className="p-4 space-y-1">
      <div className="flex items-center gap-1">
        <span className="text-xs uppercase text-text-secondary tracking-wide">{label}</span>
        {metricKey ? (
          <MetricTooltip metricKey={metricKey} value={metricValue}>
            <HelpCircle size={12} aria-hidden />
          </MetricTooltip>
        ) : resolvedGlossaryKey ? (
          <GlossaryTooltip k={resolvedGlossaryKey} ariaLabel={`About ${label}`} iconOnly>
            <span data-glossary-key={resolvedGlossaryKey} className="inline-flex text-text-muted">
              <HelpCircle size={12} aria-hidden />
            </span>
          </GlossaryTooltip>
        ) : null}
      </div>
      <div className={cn("text-2xl font-display font-semibold", mono && "font-mono tabular-nums", valueTone)}>{value}</div>
      {deltaInfo && (
        <div className="text-xs">
          <DeltaText delta={deltaInfo} />
        </div>
      )}
      {sparkline?.length ? <Sparkline data={sparkline} ariaLabel={`${label} sparkline`} /> : null}
    </Card>
  );
}

export { KpiTile as Metric };
