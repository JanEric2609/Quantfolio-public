import { formatNumber } from "../../../lib/format";
import { useRegimeWeights } from "../hooks/useRegimeWeights";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";

export function RegimeSignalsView() {
  const { data, isLoading } = useRegimeWeights();
  if (isLoading)
    return (
      <div className="p-4 text-sm text-text-secondary">Loading regime data...</div>
    );
  if (!data)
    return (
      <div className="p-4 text-sm text-text-secondary">Regime data unavailable.</div>
    );

  const label = data.regime_label ?? "unknown";

  const metrics: MetricItem[] = [
    {
      label: "Market state",
      value: label.toUpperCase(),
      mono: false,
      hint: data.available ? "statistical jump model" : (data.reason ?? "not classified yet"),
    },
    {
      label: "In this state since",
      value: data.state_since ?? "-",
      mono: false,
      hint: data.as_of ? `data to ${data.as_of}` : undefined,
    },
    { label: "Crisis", value: data.crisis ? "YES" : "NO", mono: false },
  ];
  if (data.vix != null)
    metrics.push({ label: "VIX", value: formatNumber(data.vix, { digits: 1 }), metricKey: "vix", metricValue: data.vix });

  // What the state changes, and nothing more: Discover pauses new picks in
  // bear or crisis (decision/discover/regime_gate.py); no optimiser, plan or
  // backtest reads it.
  const descriptions: Record<string, string> = {
    bull: "Bull: the jump model reads rising prices with calm volatility. Discover runs as usual.",
    bear: "Bear: falling prices with high volatility. Discover pauses new picks until the state changes.",
    sideways: "Sideways: no clear direction. Discover runs as usual.",
    unknown: "Unknown: the jump model has no recent classification. Nothing is guessed; Discover runs as usual.",
  };
  const crisisNote = data.crisis ? " VIX is above 30, so Discover pauses new picks." : "";

  return (
    <div className="space-y-0">
      <SummaryStrip metrics={metrics} />

      <div className="space-y-4 pt-4">
        {/* Description */}
        <div
          className="rounded-md p-3 text-xs text-text-secondary"
          style={{ background: "rgb(var(--c-surface))" }}
        >
          {(descriptions[label] ?? descriptions.unknown) + crisisNote}
        </div>
      </div>
    </div>
  );
}
