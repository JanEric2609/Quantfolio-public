import { useRealHoldings } from "../hooks/useRealHoldings";
import { useRealPortfolioSummary } from "../hooks/useRealPortfolioSummary";
import { useRealPortfolioRisk } from "../hooks/useRealPortfolioRisk";
import { useRealRebalance } from "../hooks/useRealRebalance";
import { DonutChart } from "../../../components/charts/DonutChart";
import { useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { RebalanceCard } from "../components/RebalanceCard";
import type { QuantRiskMetrics } from "../../../lib/api";
import { formatCurrency, formatNumber, formatPercent } from "../../../lib/format";
import { useBackfillJob } from "../hooks/useBackfillJob";
import { EmptyState } from "../../../components/shared/EmptyState";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";
import { Database } from "lucide-react";
import { BrokerBadge, BrokerFilterToggle, matchesBroker, useBrokerFilter } from "../../../components/portfolio/BrokerFilter";

const pct = (v?: number | null) => formatPercent(v, { digits: 2 });
const num = (v?: number | null) => formatNumber(v, { digits: 3 });
const fmt = (v?: number | string | null) => (v == null ? "—" : formatCurrency(Number(v), "EUR"));

export function RealHoldingsView() {
  const { data, isLoading } = useRealHoldings();
  const allPositions = data?.positions ?? [];
  const brokerFilter = useBrokerFilter(allPositions.map((p) => p.source));
  const summaryQ = useRealPortfolioSummary();
  const riskQ = useRealPortfolioRisk();
  const [params, setParams] = useSearchParams();
  const target = params.get("target") ?? "erc";
  const rebalanceQ = useRealRebalance(target);
  const qc = useQueryClient();

  const backfill = useBackfillJob(() =>
    qc.invalidateQueries({ queryKey: ["quant", "portfolio", "real"] })
  );

  if (isLoading)
    return (
      <div className="p-4 text-sm text-text-secondary">
        Loading real holdings...
      </div>
    );
  if (!data || data.position_count === 0)
    return (
      <EmptyState
        icon={Database}
        title="No synced positions found"
        description="Sync DKB or Scalable Capital first, then backfill price data to activate analytics."
        actionLabel={backfill.isRunning ? "Backfilling..." : "Backfill prices now"}
        onAction={() => backfill.start()}
      />
    );

  const filtered = brokerFilter.filter !== "all";
  const positions = allPositions.filter((p) => matchesBroker(p.source, brokerFilter.filter));
  const shownTotal = positions.reduce((sum, p) => sum + Number(p.current_value), 0);
  // One slice per fund: the same ISIN at two depots is one slice under "All".
  const sliceValues = new Map<string, { name: string; value: number }>();
  for (const p of positions) {
    const key = p.isin || p.ticker || "";
    const slice = sliceValues.get(key) ?? { name: "", value: 0 };
    slice.name = slice.name || p.ticker || p.isin || "";
    slice.value += Number(p.current_value);
    sliceValues.set(key, slice);
  }
  const slices = [...sliceValues.values()];
  const withTicker = positions.filter((p) => p.ticker).length;

  const metrics: MetricItem[] = [
    { label: filtered ? "Depot Value" : "Total Value", value: fmt(filtered ? shownTotal : data.total_value) },
    { label: "Positions", value: String(positions.length) },
    { label: "With Ticker", value: String(withTicker) },
    { label: "ISIN Only", value: String(positions.length - withTicker) },
  ];
  const depotTotals = Object.entries(data.by_broker ?? {});

  // Risk metrics from summary endpoint. `Partial<>` because the `{}`
  // fallback (no data yet) can't satisfy the full non-optional shape.
  const riskMetrics: Partial<Omit<QuantRiskMetrics, "drawdown">> = summaryQ.data?.metrics ?? {};
  const riskScorecard: MetricItem[] = [
    { label: "Sharpe", value: num(riskMetrics.sharpe) },
    { label: "Sortino", value: num(riskMetrics.sortino) },
    { label: "Max DD", value: summaryQ.data?.max_drawdown?.max_drawdown != null ? pct(summaryQ.data.max_drawdown.max_drawdown) : "-" },
    { label: "CVaR 95%", value: num(riskMetrics.historical_cvar) },
    { label: "Beta", value: num(riskMetrics.beta) },
    { label: "Alpha", value: riskMetrics.alpha != null ? pct(riskMetrics.alpha) : "-" },
  ];

  // Factor exposures
  const exposures = riskQ.data?.factor_exposures ?? {};
  const factorEntries = Object.entries(exposures);


  return (
    <div className="space-y-0">
      {/* Summary Strip */}
      <SummaryStrip metrics={metrics} />

      <div className="space-y-4 pt-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <BrokerFilterToggle brokers={brokerFilter.brokers} value={brokerFilter.filter} onChange={brokerFilter.setFilter} />
          <span className="ml-auto text-xs text-text-muted">
            Positions are snapshotted and tickers resolved after every sync and each evening.
          </span>
        </div>

        {depotTotals.length > 1 && (
          <section className="rounded-md p-3 bg-surface" aria-label="Per depot">
            <h2 className="mb-2 text-sm font-semibold text-text-primary">Per depot</h2>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              {depotTotals.map(([source, depot]) => (
                <div key={source} className="rounded p-2 text-xs bg-surface-2">
                  <div className="flex items-center gap-1.5">
                    <BrokerBadge source={source} />
                    <span className="text-text-secondary">{depot.position_count} positions · {pct(depot.weight)}</span>
                  </div>
                  <div className="mt-1 text-sm font-medium" style={{ fontFamily: '"JetBrains Mono", monospace' }}>{fmt(depot.total_value)}</div>
                  <DepotPnl depot={depot} />
                </div>
              ))}
              <div className="rounded p-2 text-xs bg-surface-2">
                <div className="text-text-secondary">All depots</div>
                <div className="mt-1 text-sm font-medium" style={{ fontFamily: '"JetBrains Mono", monospace' }}>{fmt(data.total_value)}</div>
                <DepotPnl
                  depot={depotTotals.reduce(
                    (sum, [, d]) => ({
                      cost_basis: sum.cost_basis + d.cost_basis,
                      costed_value: sum.costed_value + d.costed_value,
                      unrealized_pnl: sum.unrealized_pnl + d.unrealized_pnl,
                      total_value: sum.total_value + d.total_value,
                    }),
                    { cost_basis: 0, costed_value: 0, unrealized_pnl: 0, total_value: 0 },
                  )}
                />
              </div>
            </div>
          </section>
        )}

        {slices.length > 0 && (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-[1.4fr_1fr]">
            <section className="rounded-md p-3 bg-surface">
              <h2 className="mb-2 text-sm font-semibold text-text-primary">
                Allocation by Ticker
              </h2>
              <DonutChart
                ariaLabel="Real holdings allocation"
                className="h-72"
                slices={slices}
              />
            </section>
            <section className="rounded-md p-3 bg-surface">
              <h2 className="mb-2 text-sm font-semibold text-text-primary">
                Positions
              </h2>
              <div className="max-h-72 space-y-1 overflow-y-auto">
                {positions.map((p) => {
                  // Relative to the depot shown when filtered, so the share
                  // and its concentration warning read the same weight.
                  const weight = filtered
                    ? (shownTotal ? Number(p.current_value) / shownTotal : 0)
                    : p.weight;
                  return (
                  <div
                    key={`${p.source ?? ""}-${p.account_id ?? ""}-${p.isin ?? p.ticker}`}
                    className="flex items-center justify-between gap-2 rounded px-2 py-1 text-xs bg-surface-2"
                  >
                    <div className="flex min-w-0 flex-1 items-center gap-1.5">
                      <BrokerBadge source={p.source} />
                      <span className="truncate">
                        <span className="font-medium">
                          {p.ticker || "ISIN"}
                        </span>{" "}
                        <span className="text-text-secondary">{p.name}</span>
                      </span>
                    </div>
                    <div className="shrink-0 text-right">
                      <div style={{ fontFamily: '"JetBrains Mono", monospace' }}>
                        {fmt(p.current_value)}
                      </div>
                      <div className={Number(weight ?? 0) > 0.1 ? "text-warn" : "text-text-muted"}>
                        {pct(weight)}
                      </div>
                    </div>
                  </div>
                  );
                })}
              </div>
            </section>
          </div>
        )}

        {filtered && (
          <p className="text-xs text-text-muted">
            The risk metrics, factor exposures and rebalancing below cover every depot together.
          </p>
        )}

        {/* Risk Scorecard */}
        <section className="rounded-md p-3 bg-surface">
          <h2 className="mb-2 text-sm font-semibold text-text-primary">
            Risk Metrics
            {summaryQ.isLoading && (
              <span className="ml-2 text-xs text-text-muted">Loading...</span>
            )}
          </h2>
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
            {riskScorecard.map((m) => (
              <div
                key={m.label}
                className="rounded p-2 text-center bg-surface-2"
              >
                <div className="text-[10px] text-text-muted">{m.label}</div>
                <div
                  className="mt-0.5 text-sm font-medium"
                  style={{ fontFamily: '"JetBrains Mono", monospace' }}
                >
                  {m.value}
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* Factor Exposures */}
        {factorEntries.length > 0 && (
          <section className="rounded-md p-3 bg-surface">
            <h2 className="mb-2 text-sm font-semibold text-text-primary">
              Factor Exposures
              {riskQ.isLoading && (
                <span className="ml-2 text-xs text-text-muted">Loading...</span>
              )}
            </h2>
            <div className="space-y-2">
              {factorEntries.map(([name, value]) => {
                const v = Number(value);
                const maxBar = 1.5;
                const width = Math.min(Math.abs(v) / maxBar, 0.5) * 100;
                const isPositive = v >= 0;
                return (
                  <div key={name} className="flex items-center gap-2 text-xs">
                    <span className="w-20 text-right text-text-secondary">{name}</span>
                    <div className="flex-1">
                      <div className="relative h-4 rounded bg-surface-2">
                        <div
                          className="absolute top-0 h-full rounded"
                          style={{
                            width: `${width}%`,
                            left: isPositive ? "50%" : undefined,
                            right: isPositive ? undefined : "50%",
                            background: isPositive ? "rgb(var(--c-success))" : "rgb(var(--c-danger))",
                            opacity: 0.6,
                          }}
                        />
                        {/* Zero line */}
                        <div className="absolute top-0 h-full w-px bg-border" style={{ left: "50%" }} />
                      </div>
                    </div>
                    <span
                      className="w-16 text-right"
                      style={{
                        fontFamily: '"JetBrains Mono", monospace',
                        color: isPositive ? "rgb(var(--c-success))" : "rgb(var(--c-danger))",
                      }}
                    >
                      {formatNumber(v, { digits: 3, signed: true })}
                    </span>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* Band rebalance toward a covariance-only target */}
        <RebalanceCard
          data={rebalanceQ.data}
          loading={rebalanceQ.isLoading}
          target={target}
          onTarget={(t) =>
            setParams((old) => {
              old.set("target", t);
              return old;
            })
          }
        />
      </div>
    </div>
  );
}

function DepotPnl({ depot }: { depot: { cost_basis: number; costed_value: number; unrealized_pnl: number; total_value: number } }) {
  if (depot.cost_basis <= 0) return <div className="text-text-muted">No purchase cost known</div>;
  const ratio = depot.unrealized_pnl / depot.cost_basis;
  return (
    <div>
      <span className={depot.unrealized_pnl >= 0 ? "text-success" : "text-danger"}>
        {formatCurrency(depot.unrealized_pnl, { currency: "EUR", signed: true })} ({formatPercent(ratio, { digits: 1, signed: true })})
      </span>{" "}
      <span className="text-text-muted">unrealised</span>
      {depot.costed_value < depot.total_value - 0.005 && (
        <div className="text-text-muted">covers the {fmt(depot.costed_value)} with a known cost</div>
      )}
    </div>
  );
}
