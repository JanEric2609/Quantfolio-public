import { useState, useEffect } from "react";
import { Card } from "../../components/ui/card";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { GlossaryTooltip } from "../../components/composed/GlossaryTooltip";
import { LineChart } from "../../components/charts/LineChart";
import { formatDate, formatNumber, formatPercent } from "../../lib/format";
import {
  useComposites,
  useLedgerBenchmark,
  useLedgerEntries,
  useLedgerSummary,
  useWriteLedgerSnapshot,
} from "../quantlab/hooks/usePerformance";
import { BenchmarkCard } from "./components/BenchmarkCard";

const fmtPct = (v: number | null | undefined) => formatPercent(v, { digits: 2 });

export function PerformanceTab() {
  const [selectedComposite, setSelectedComposite] = useState<string | null>(null);

  const composites = useComposites();
  const ledgerSummary = useLedgerSummary(selectedComposite);
  const writeSnapshot = useWriteLedgerSnapshot();
  const benchmark = useLedgerBenchmark(selectedComposite);

  useEffect(() => {
    const list = composites.data?.research?.composites;
    if (!selectedComposite && list && list.length > 0) {
      setSelectedComposite(list[0].id);
    }
  }, [composites.data, selectedComposite]);

  const handleSnapshot = () => {
    if (!selectedComposite) return;
    writeSnapshot.mutate({
      composite_id: selectedComposite,
      as_of: new Date().toISOString(),
    });
  };

  const ledgerEntries = useLedgerEntries(selectedComposite, 120);
  const entries = ledgerEntries.data?.research?.entries ?? [];
  // API returns newest-first; the chart needs chronological order.
  const chronological = [...entries].reverse();
  const summary = ledgerSummary.data;
  const research = summary?.research;

  if (composites.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-40 w-full rounded-md" />
        <Skeleton className="h-60 w-full rounded-md" />
      </div>
    );
  }

  if (composites.isError) {
    return (
      <Card className="border border-line bg-panel p-4">
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm text-danger">Failed to load performance composites.</div>
          <Button variant="outline" size="sm" onClick={() => composites.refetch()}>Retry</Button>
        </div>
      </Card>
    );
  }

  const compositeList = composites.data?.research?.composites || [];
  const hasComposites = compositeList.length > 0;

  if (!hasComposites) {
    return (
      <Card className="border border-line bg-panel p-4">
        <div className="text-sm text-text-muted">
          No composites available. Please check your portfolio setup.
        </div>
      </Card>
    );
  }

  const selectedName =
    compositeList.find((c) => c.id === selectedComposite)?.name ?? compositeList[0]?.name;

  return (
    <div className="space-y-4">
      {/* Action Bar */}
      <section className="rounded-md border border-line bg-panel p-4">
        <div className="flex items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-text-primary">
              {selectedName || "Performance"}
            </h3>
            <p className="text-xs text-text-muted">
              <GlossaryTooltip k="composite">Composite performance ledger</GlossaryTooltip>
            </p>
          </div>
          <Button
            onClick={handleSnapshot}
            disabled={writeSnapshot.isPending}
            className="shrink-0"
          >
            {writeSnapshot.isPending ? "Saving..." : "Snapshot Ledger"}
          </Button>
        </div>
        {writeSnapshot.isError && (
          <p role="alert" className="mt-2 text-xs text-danger">
            Snapshot failed: {(writeSnapshot.error as Error)?.message ?? "Unknown error"}
          </p>
        )}
        {writeSnapshot.isSuccess && (
          <p className="mt-2 text-xs text-success">
            Snapshot written — TWR/MWR recomputed from your portfolio history.
          </p>
        )}
      </section>

      {/* Performance Metrics */}
      {research && (
        <section className="rounded-md border border-line bg-panel p-4">
          <h3 className="mb-4 text-sm font-semibold">Latest Performance</h3>

          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            <div>
              <div className="text-xs text-text-muted">
                <GlossaryTooltip k="twr">TWR</GlossaryTooltip>
              </div>
              <div className="font-mono text-sm font-semibold text-success">
                {fmtPct(research.twr)}
              </div>
            </div>

            <div>
              <div className="text-xs text-text-muted">
                <GlossaryTooltip k="mwr">MWR</GlossaryTooltip>
              </div>
              <div className="font-mono text-sm font-semibold text-success">
                {fmtPct(research.mwr)}
              </div>
            </div>

            {research.dispersion != null && (
              <div>
                <div className="text-xs text-text-muted">
                  <GlossaryTooltip k="dispersion">Dispersion</GlossaryTooltip>
                </div>
                <div className="font-mono text-sm text-text-secondary">
                  {fmtPct(research.dispersion)}
                </div>
              </div>
            )}

            {research.ex_post_risk?.sharpe != null && (
              <div>
                <div className="text-xs text-text-muted">
                  <GlossaryTooltip k="sharpe">Sharpe</GlossaryTooltip>
                </div>
                <div className="font-mono text-sm text-text-secondary">
                  {formatNumber(research.ex_post_risk.sharpe, { digits: 2 })}
                </div>
              </div>
            )}
          </div>

          {/* Risk Breakdown */}
          {research.ex_post_risk && (
            <div className="mt-4 border-t border-line pt-4">
              <h4 className="mb-2 text-xs font-semibold text-text-primary">Ex-Post Risk</h4>
              <div className="grid grid-cols-3 gap-2 text-xs">
                <div className="flex justify-between">
                  <span className="text-text-muted">Volatility</span>
                  <span className="font-mono text-text-primary">
                    {fmtPct(research.ex_post_risk.volatility)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">Max Drawdown</span>
                  <span className="font-mono text-text-primary">
                    {fmtPct(research.ex_post_risk.max_drawdown)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">CVaR 95%</span>
                  <span className="font-mono text-text-primary">
                    {fmtPct(research.ex_post_risk.cvar_95)}
                  </span>
                </div>
              </div>
            </div>
          )}
        </section>
      )}

      {/* vs benchmark: the passive core ETF over the same window */}
      {benchmark.data?.research ? <BenchmarkCard benchmark={benchmark.data.research} /> : null}

      {/* TWR History Chart */}
      {chronological.length > 1 && (
        <section className="rounded-md border border-line bg-panel p-4">
          <h3 className="mb-3 text-sm font-semibold">
            <GlossaryTooltip k="twr">TWR History</GlossaryTooltip>
          </h3>
          <LineChart
            ariaLabel="TWR history"
            className="h-64"
            series={[
              {
                name: "TWR",
                data: chronological.map((e) => [e.as_of, e.twr]),
              },
            ]}
          />
        </section>
      )}

      {/* Ledger Entries */}
      <section className="rounded-md border border-line bg-panel p-4">
        <h3 className="mb-3 text-sm font-semibold">Ledger Entries</h3>
        {ledgerEntries.isLoading ? (
          <Skeleton className="h-24 w-full rounded-md" />
        ) : entries.length === 0 ? (
          <div className="text-sm text-text-muted">
            <p>No ledger entries yet.</p>
            <p className="mt-1 text-xs">
              Click <strong>Snapshot Ledger</strong> to record the first entry. TWR/MWR need at
              least 2 daily portfolio snapshots — snapshots accumulate automatically once the
              worker's daily position snapshot job runs (or after DKB syncs on consecutive days).
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-text-muted">
                  <th className="py-2 pr-4">As of</th>
                  <th className="py-2 pr-4 text-right">TWR</th>
                  <th className="py-2 pr-4 text-right">MWR</th>
                  <th className="py-2 pr-4 text-right">Dispersion</th>
                  <th className="py-2 text-right">Snapshots used</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => (
                  <tr key={e.id} className="border-b border-line last:border-b-0">
                    <td className="py-2 pr-4 font-mono text-text-secondary">
                      {formatDate(e.as_of)}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-text-primary">
                      {fmtPct(e.twr)}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-text-primary">
                      {fmtPct(e.mwr)}
                    </td>
                    <td className="py-2 pr-4 text-right font-mono text-text-secondary">
                      {fmtPct(e.dispersion)}
                    </td>
                    <td className="py-2 text-right font-mono text-text-secondary">
                      {e.snapshot_meta?.snapshots_used ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Education Note */}
      <Card className="border border-line bg-panel p-3">
        <p className="text-xs text-text-secondary">
          <strong>TWR</strong> (Time-Weighted Return) measures portfolio performance independent of cashflows, suitable for manager evaluation.
          <br />
          <strong>MWR</strong> (Money-Weighted Return) accounts for timing and size of cashflows, reflecting investor experience.
        </p>
      </Card>
    </div>
  );
}
