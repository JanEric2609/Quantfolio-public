import { formatNumber, formatPercent } from "../../../lib/format";
import { useState } from "react";
import { useFactorExposures } from "../hooks/useFactorExposures";
import { useFactorRotation } from "../hooks/useFactorRotation";
import { useSmartBetaComparison } from "../hooks/useSmartBetaComparison";
import { MetricTooltip } from "../../../components/composed/MetricTooltip";

interface FactorExposureItem {
  name: string;
  score?: number;
  weight?: number;
  benchmark_weight?: number;
}

interface TechnicalSignalItem {
  name: string;
  value: number | string;
  signal?: string;
}

interface RotationFactorItem {
  name: string;
  return_30d?: number;
  return_60d?: number;
  return_90d?: number;
  momentum: number;
}

interface SmartBetaEtfItem {
  ticker: string;
  factor?: string;
  tracking_error?: number;
  overlap_score?: number;
}

function SectionHeading({ title, metricKey }: { title: string; metricKey: string }) {
  return (
    <h3 className="mb-2 flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wider text-text-secondary">
      {title}
      <MetricTooltip metricKey={metricKey} />
    </h3>
  );
}

const FACTOR_COLORS: Record<string, string> = {
  market: "bg-blue-500",
  size: "bg-purple-500",
  value: "bg-warn",
  momentum: "bg-success",
  quality: "bg-cyan-500",
  low_vol: "bg-pink-500",
};

function FactorBar({ name, score, benchmark }: { name: string; score: number; benchmark?: number }) {
  const color = FACTOR_COLORS[name] || "bg-slate-500";
  const pct = Math.min(Math.max(score * 100, -100), 100);
  const benchPct = benchmark != null ? Math.min(Math.max(benchmark * 100, -100), 100) : null;
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs">
        <span className="capitalize text-text-primary">{name.replace("_", " ")}</span>
        <span className="font-mono text-text-secondary">{formatNumber(score, { digits: 2 })}</span>
      </div>
      <div className="relative h-2 rounded-full bg-surface-2">
        <div className={`absolute inset-y-0 left-0 rounded-full ${color}`} style={{ width: `${pct}%` }} />
        {benchPct != null && (
          <div className="absolute top-0 bottom-0 w-0.5 bg-white/40" style={{ left: `${benchPct}%` }} />
        )}
      </div>
    </div>
  );
}

function SignalBadge({ signal }: { signal: string }) {
  const cls =
    signal === "bullish"
      ? "bg-success/20 text-success"
      : signal === "bearish"
        ? "bg-danger/20 text-danger"
        : "bg-surface-2 text-text-primary";
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${cls}`}>{signal}</span>;
}

export function FactorDashboardView() {
  const [rotationWindow, setRotationWindow] = useState(60);
  const { data: factors, isLoading: factorsLoading } = useFactorExposures();
  const { data: rotation, isLoading: rotationLoading } = useFactorRotation(rotationWindow);
  const { data: smartBeta, isLoading: betaLoading } = useSmartBetaComparison();

  if (factorsLoading) {
    return <div className="p-4 text-xs text-text-secondary">Loading factor data...</div>;
  }

  const exposures = factors?.factor_exposures ?? [];
  const signals = factors?.technical_signals ?? [];
  const rotationFactors = rotation?.factors ?? [];
  const etfs = smartBeta?.etfs ?? [];

  return (
    <div className="space-y-4 p-4 text-xs">
      {/* Factor Exposure Cards */}
      <section>
        <SectionHeading title="Factor Exposures" metricKey="factor_exposure" />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {exposures.map((f: FactorExposureItem) => (
            <div key={f.name} className="rounded-md border border-line bg-panel p-3">
              <FactorBar name={f.name} score={f.score ?? 0} benchmark={f.benchmark_weight} />
              {f.weight != null && (
                <div className="mt-1 text-[10px] text-text-muted">
                  Portfolio: {formatPercent(f.weight, { digits: 1 })}
                  {f.benchmark_weight != null && ` | Benchmark: ${formatPercent(f.benchmark_weight, { digits: 1 })}`}
                </div>
              )}
            </div>
          ))}
          {exposures.length === 0 && (
            <div className="col-span-full rounded-md border border-line bg-panel p-3 text-center text-text-muted">
              No factor data available. Add holdings to see factor exposures.
            </div>
          )}
        </div>
      </section>

      {/* Technical Signals */}
      <section>
        <SectionHeading title="Technical Signals" metricKey="technical_signals" />
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3 lg:grid-cols-5">
          {signals.map((s: TechnicalSignalItem) => (
            <div key={s.name} className="flex items-center justify-between rounded-md border border-line bg-panel px-3 py-2">
              <span className="text-text-primary">{s.name}</span>
              <div className="flex items-center gap-2">
                <span className="font-mono text-text-secondary">{typeof s.value === "number" ? formatNumber(s.value, { digits: 2 }) : s.value}</span>
                <SignalBadge signal={s.signal ?? "neutral"} />
              </div>
            </div>
          ))}
          {signals.length === 0 && (
            <div className="col-span-full rounded-md border border-line bg-panel p-3 text-center text-text-muted">
              No technical signals available.
            </div>
          )}
        </div>
      </section>

      {/* Factor Rotation */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wider text-text-secondary">
            Factor Rotation
            <MetricTooltip metricKey="factor_rotation" />
          </h3>
          <div className="flex gap-1">
            {[30, 60, 90].map((w) => (
              <button
                key={w}
                type="button"
                onClick={() => setRotationWindow(w)}
                className={`rounded px-2 py-0.5 text-[10px] transition-colors ${
                  rotationWindow === w
                    ? "bg-accent/20 text-accent"
                    : "text-text-muted hover:text-text-primary"
                }`}
              >
                {w}d
              </button>
            ))}
          </div>
        </div>
        {rotationLoading ? (
          <div className="text-text-muted">Loading rotation data...</div>
        ) : (
          <div className="overflow-x-auto rounded-md border border-line">
            <table className="w-full">
              <thead>
                <tr className="border-b border-line bg-surface text-[10px] uppercase text-text-muted">
                  <th className="px-3 py-1.5 text-left">Factor</th>
                  <th className="px-3 py-1.5 text-right">30d</th>
                  <th className="px-3 py-1.5 text-right">60d</th>
                  <th className="px-3 py-1.5 text-right">90d</th>
                  <th className="px-3 py-1.5 text-right">Momentum</th>
                </tr>
              </thead>
              <tbody>
                {rotationFactors.map((f: RotationFactorItem) => (
                  <tr key={f.name} className="border-b border-line/50 text-text-primary">
                    <td className="px-3 py-1.5 capitalize">{f.name.replace("_", " ")}</td>
                    <td className={`px-3 py-1.5 text-right font-mono ${(f.return_30d ?? 0) >= 0 ? "text-success" : "text-danger"}`}>
                      {formatPercent(f.return_30d ?? 0, { digits: 1 })}
                    </td>
                    <td className={`px-3 py-1.5 text-right font-mono ${(f.return_60d ?? 0) >= 0 ? "text-success" : "text-danger"}`}>
                      {formatPercent(f.return_60d ?? 0, { digits: 1 })}
                    </td>
                    <td className={`px-3 py-1.5 text-right font-mono ${(f.return_90d ?? 0) >= 0 ? "text-success" : "text-danger"}`}>
                      {formatPercent(f.return_90d ?? 0, { digits: 1 })}
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      <SignalBadge signal={f.momentum > 0 ? "bullish" : f.momentum < 0 ? "bearish" : "neutral"} />
                    </td>
                  </tr>
                ))}
                {rotationFactors.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-3 py-4 text-center text-text-muted">
                      No rotation data available.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Smart Beta ETF Comparison */}
      <section>
        <SectionHeading title="Smart Beta ETF Comparison" metricKey="smart_beta" />
        {betaLoading ? (
          <div className="text-text-muted">Loading smart beta data...</div>
        ) : (
          <div className="overflow-x-auto rounded-md border border-line">
            <table className="w-full">
              <thead>
                <tr className="border-b border-line bg-surface text-[10px] uppercase text-text-muted">
                  <th className="px-3 py-1.5 text-left">ETF</th>
                  <th className="px-3 py-1.5 text-left">Factor</th>
                  <th className="px-3 py-1.5 text-right">Tracking Error</th>
                  <th className="px-3 py-1.5 text-right">Overlap</th>
                </tr>
              </thead>
              <tbody>
                {etfs.map((e: SmartBetaEtfItem) => (
                  <tr key={e.ticker} className="border-b border-line/50 text-text-primary">
                    <td className="px-3 py-1.5 font-mono">{e.ticker}</td>
                    <td className="px-3 py-1.5 capitalize">{(e.factor ?? "").replace("_", " ")}</td>
                    <td className="px-3 py-1.5 text-right font-mono">
                      {e.tracking_error != null ? formatPercent(e.tracking_error, { digits: 2 }) : "—"}
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono">
                      {e.overlap_score != null ? formatPercent(e.overlap_score, { digits: 0 }) : "—"}
                    </td>
                  </tr>
                ))}
                {etfs.length === 0 && (
                  <tr>
                    <td colSpan={4} className="px-3 py-4 text-center text-text-muted">
                      No smart beta comparison data available.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
