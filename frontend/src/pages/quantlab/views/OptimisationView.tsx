import { useState } from "react";
import { Link } from "react-router-dom";
import { XCircle } from "lucide-react";
import { formatNumber, formatPercent } from "../../../lib/format";
import { Skeleton } from "../../../components/ui/skeleton";
import { EmptyState } from "../../../components/shared/EmptyState";
import { BarChart } from "../../../components/charts/BarChart";
import { useQuantOptimisation } from "../hooks/useQuantOptimisation";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";
import type { AllocationMethod } from "../../../lib/api";

const pct = (v?: number | null, digits = 1) => formatPercent(v, { digits });
const ORDER = ["equal", "inverse_vol", "min_variance", "erc", "hrp"];

const WHY: Record<string, string> = {
  equal: "Every line the same weight. Hard to beat out of sample (DeMiguel, Garlappi and Uppal 2009).",
  inverse_vol: "Calmer lines get more weight; ignores correlation.",
  min_variance: "The lowest volatility the covariance allows; can concentrate in one or two lines.",
  erc: "Each line adds the same share of risk (Maillard, Roncalli and Teiletche 2010).",
  hrp: "Clusters similar lines first, then splits risk between clusters (López de Prado 2016).",
};

/**
 * Target allocations from the covariance matrix alone. No expected returns:
 * estimated means are the largest source of error in optimisation, and for a
 * few broad ETFs a mean-based optimum is mostly noise. Each card shows the
 * risk it would carry and how far it is from today's book; "Rebalance toward"
 * opens the band rebalance on the Holdings tab.
 */
export function OptimisationView() {
  const [cap, setCap] = useState<number | undefined>(undefined);
  const optimisation = useQuantOptimisation(cap);

  if (optimisation.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-16 w-full" />
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {[1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-40 w-full" />
          ))}
        </div>
      </div>
    );
  }
  if (optimisation.error) {
    return (
      <EmptyState
        icon={XCircle}
        title="Failed to load optimisation data"
        description={optimisation.error instanceof Error ? optimisation.error.message : "An unexpected error occurred."}
      />
    );
  }
  const data = optimisation.data;
  if (!data?.available) {
    return (
      <div className="rounded-md p-3 text-sm text-text-secondary bg-surface">
        {data?.reason ?? "Portfolio price history is needed for optimisation."}
      </div>
    );
  }
  const methods = ORDER.filter((k) => data.methods?.[k]).map((k) => [k, data.methods![k]] as [string, AllocationMethod]);
  const assets = data.assets ?? [];
  const current = data.current;
  const cov = data.covariance;

  const metrics: MetricItem[] = [
    { label: "Your volatility", value: pct(current?.volatility), hint: "a year, Ledoit-Wolf covariance" },
    {
      label: "Diversification ratio",
      value: formatNumber(current?.diversification_ratio, { digits: 2 }),
      hint: "1 = no diversification benefit",
    },
    { label: "Effective lines", value: formatNumber(current?.effective_n, { digits: 1 }), hint: `of ${assets.length}` },
    { label: "History", value: cov ? `${cov.samples} days` : "-", hint: cov ? `${cov.start} – ${cov.end}` : undefined },
  ];

  return (
    <div className="space-y-0">
      <SummaryStrip metrics={metrics} />
      <div className="space-y-4 pt-4">
        <section className="flex flex-wrap items-center gap-3 rounded-md p-3 text-xs bg-surface">
          <label className="flex items-center gap-1">
            <span className="text-text-muted">Cap per line</span>
            <select
              className="rounded border border-line bg-surface px-2 py-1"
              value={cap ?? ""}
              onChange={(e) => setCap(e.target.value ? Number(e.target.value) : undefined)}
            >
              <option value="">none</option>
              <option value="0.6">60 %</option>
              <option value="0.4">40 %</option>
              <option value="0.25">25 %</option>
            </select>
          </label>
          <span className="text-text-muted">
            EUR daily returns, covariance shrunk toward a scaled identity (Ledoit-Wolf, intensity{" "}
            {cov ? formatNumber(cov.shrinkage, { digits: 2 }) : "-"}). Long-only, fully invested. No expected returns are
            used.
          </span>
        </section>

        {current && (
          <section className="rounded-md p-3 bg-surface">
            <h2 className="mb-1 text-sm font-semibold text-text-primary">Where your risk comes from today</h2>
            <p className="mb-2 text-[11px] text-text-muted">
              Share of the book's variance each line contributes; weight and risk differ when lines differ in
              volatility or move together.
            </p>
            <BarChart
              ariaLabel="Weight and risk contribution per line"
              className="h-56"
              categories={assets}
              series={[
                { name: "Weight", data: assets.map((a) => 100 * (current.weights[a] ?? 0)) },
                { name: "Risk contribution", data: assets.map((a) => 100 * (current.risk_contributions[a] ?? 0)) },
              ]}
              yName="% of book"
              yFormat={(v) => `${Math.round(v)} %`}
            />
          </section>
        )}

        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {methods.map(([key, m]) => (
            <article key={key} className="rounded-md border border-line bg-panel p-3 text-sm" data-testid={`method-${key}`}>
              <h3 className="font-semibold">{m.label}</h3>
              <p className="mt-0.5 text-[11px] text-text-muted">{WHY[key]}</p>
              <div className="mt-2 grid grid-cols-3 gap-1 text-xs text-text-secondary">
                <span>Volatility {pct(m.volatility)}</span>
                <span>Div. ratio {formatNumber(m.diversification_ratio, { digits: 2 })}</span>
                <span>Turnover {pct(m.turnover)}</span>
              </div>
              <table className="mt-2 w-full text-xs">
                <thead className="text-text-muted">
                  <tr>
                    <th className="text-left font-normal">Line</th>
                    <th className="text-right font-normal">Now</th>
                    <th className="text-right font-normal">Target</th>
                    <th className="text-right font-normal">Risk share</th>
                  </tr>
                </thead>
                <tbody>
                  {assets.map((a) => (
                    <tr key={a}>
                      <td className="font-mono">{a}</td>
                      <td className="text-right font-mono tabular-nums">{pct(data.weights_current?.[a])}</td>
                      <td className="text-right font-mono tabular-nums">{pct(m.weights[a])}</td>
                      <td className="text-right font-mono tabular-nums">{pct(m.risk_contributions[a])}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <Link
                to={`/quantlab/holdings?target=${key}`}
                className="mt-3 inline-block text-xs text-accent hover:underline"
              >
                Rebalance toward this →
              </Link>
            </article>
          ))}
        </div>
        {Object.keys(data.errors ?? {}).length > 0 && (
          <p className="text-xs text-warn">
            Not solved: {Object.entries(data.errors!).map(([k, v]) => `${k} (${v})`).join("; ")}
          </p>
        )}
        <p className="text-xs text-text-muted">
          Your actual monthly plan only changes its targets once evidence passes (This month). These are what-if targets
          for the lines you hold.
        </p>
      </div>
    </div>
  );
}
