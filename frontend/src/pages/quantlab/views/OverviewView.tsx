import { formatCurrency, formatNumber } from "../../../lib/format";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useBackfillJob } from "../hooks/useBackfillJob";
import { AreaChart } from "../../../components/charts/AreaChart";
import { DonutChart } from "../../../components/charts/DonutChart";
import { LineChart } from "../../../components/charts/LineChart";
import { Skeleton } from "../../../components/ui/skeleton";
import { EmptyState } from "../../../components/shared/EmptyState";
import { useQuantSummary } from "../hooks/useQuantSummary";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";
import { BarChart3, XCircle } from "lucide-react";
import { formatMetricPercent } from "../../../components/composed/MetricTile";
import { useBookPerformance } from "../hooks/useBookPerformance";

const pct = (value: number | null | undefined) => formatMetricPercent(value, 2, "-");

/**
 * The book's own return: a time-weighted unit value from the daily position
 * snapshots of every broker (book_performance.py), its money-weighted return
 * and the benchmark rebased to the same start. It replaces the old curve of
 * today's weights applied to two years of prices, in which every purchase
 * counted as return.
 */
export function OverviewView() {
  const summary = useQuantSummary();
  const book = useBookPerformance();
  const [logScale, setLogScale] = useState(false);
  const qc = useQueryClient();
  const backfill = useBackfillJob(() =>
    qc.invalidateQueries({ queryKey: ["quant", "portfolio", "summary"] })
  );

  if (summary.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-72 w-full" />
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-[1.4fr_1fr]">
          <Skeleton className="h-56 w-full" />
          <Skeleton className="h-56 w-full" />
        </div>
      </div>
    );
  }

  if (summary.error) {
    return (
      <EmptyState
        icon={XCircle}
        title="Failed to load portfolio summary"
        description={
          summary.error instanceof Error
            ? summary.error.message
            : "An unexpected error occurred."
        }
      />
    );
  }

  if (!summary.data?.available) {
    const diagnostics = summary.data?.diagnostics;
    const emptyLines: string[] = [];
    emptyLines.push(
      summary.data?.message ?? "Add holdings with price history to activate QuantLab."
    );
    const priced = diagnostics?.priced_assets?.length ?? 0;
    const considered = priced + (diagnostics?.missing_history?.length ?? 0);
    if (considered > 0) {
      emptyLines.push(`${priced} of ${considered} holdings have price history.`);
    }
    if (diagnostics?.missing_history?.length) {
      emptyLines.push(`Missing history for: ${diagnostics.missing_history.join(", ")}.`);
    }
    if (diagnostics?.dkb_positions_without_symbol_mapping) {
      emptyLines.push(
        `${diagnostics.dkb_positions_without_symbol_mapping} synced positions have no symbol mapping — add an ISIN→ticker override in Control Center.`
      );
    }
    if (
      backfill.job &&
      (backfill.job.status === "succeeded" || backfill.job.status === "failed")
    ) {
      const j = backfill.job;
      emptyLines.push(
        `Last backfill: ${j.succeeded ?? 0}/${j.total ?? 0} succeeded${
          j.failed ? `, ${j.failed} failed` : ""
        }.`
      );
    }
    if (backfill.error) {
      emptyLines.push(`Backfill error: ${backfill.error}`);
    }
    const emptyDescription = emptyLines.join(" ");

    return (
      <EmptyState
        icon={BarChart3}
        title="No portfolio data available"
        description={emptyDescription}
        actionLabel={
          backfill.isRunning ? "Backfilling..." : "Backfill prices now"
        }
        onAction={() => backfill.start()}
      />
    );
  }

  const perf = book.data;
  const series = perf?.available ? perf.series : [];
  const unit = series.map((p) => [p.date, p.unit_value] as [string, number]);
  const bench = series
    .filter((p) => p.benchmark != null)
    .map((p) => [p.date, p.benchmark as number] as [string, number]);
  let peak = 0;
  const drawdown = series.map((p) => {
    peak = Math.max(peak, p.unit_value);
    return [p.date, peak > 0 ? p.unit_value / peak - 1 : 0] as [string, number];
  });
  const maxDd = drawdown.reduce((m, [, v]) => Math.min(m, v), 0);
  const days = perf?.days ?? 0;
  const yearly = days >= 365;
  const periodHint = perf?.available ? `${perf.start} – ${perf.end}${yearly ? ", a year" : ""}` : undefined;

  const metrics: MetricItem[] = [
    {
      label: yearly ? "Return a year (TWR)" : "Return (TWR)",
      value: pct(yearly ? perf?.twr_annualised : perf?.twr),
      hint: perf?.available ? (yearly ? "time-weighted, annualised" : `since ${perf.start}, not annualised`) : undefined,
    },
    {
      label: yearly ? "Money-weighted a year" : "Money-weighted (IRR)",
      value: pct(yearly ? perf?.irr_annualised : perf?.irr_period),
      hint: perf?.available ? "what your own deposits earned, timing included" : undefined,
    },
    {
      label: `${perf?.benchmark ?? "Benchmark"} (EUR)`,
      value: pct(yearly ? perf?.benchmark_annualised : perf?.benchmark_return),
      hint: periodHint,
    },
    { label: "Max drawdown", value: series.length ? pct(maxDd) : "-", metricKey: "max_drawdown", metricValue: series.length ? maxDd : undefined },
    {
      label: "Money put in",
      value: perf?.available ? formatCurrency(perf.net_contributions_eur ?? 0, "EUR") : "-",
      hint: perf?.available ? `value ${formatCurrency(perf.value_end_eur ?? 0, "EUR")}` : undefined,
    },
  ];

  return (
    <div className="space-y-0">
      <SummaryStrip metrics={metrics} />

      <div className="space-y-4 pt-4">
        {summary.data?.available && summary.data?.diagnostics?.partial_data && (
          <div className="rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
            Some holdings have no price history: {summary.data.diagnostics?.missing_history?.join(", ")}.
          </div>
        )}
        {perf?.available && (perf.unpriced?.length ?? 0) > 0 && (
          <div className="rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
            No price for {perf.unpriced!.join(", ")}; left out of the return.
          </div>
        )}
        <section className="rounded-md p-3 bg-surface">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-text-primary">Your book, time-weighted (start = 100)</h2>
              <p className="text-[11px] text-text-muted">
                DKB and Scalable together, in EUR. Purchases and sales are money in or out, never return; the
                benchmark is rebased to the same start.
              </p>
            </div>
            <div className="flex rounded border border-line p-0.5">
              {[
                { label: "Linear", value: false },
                { label: "Log", value: true },
              ].map((opt) => (
                <button
                  key={opt.label}
                  onClick={() => setLogScale(opt.value)}
                  className="rounded px-2 py-0.5 text-[10px] uppercase transition-colors"
                  style={{
                    background: logScale === opt.value ? "rgb(var(--c-surface-2))" : undefined,
                    color: logScale === opt.value ? "rgb(var(--c-accent))" : "rgb(var(--c-text-muted))",
                  }}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>
          {perf && !perf.available ? (
            <p className="py-8 text-center text-sm text-text-secondary">{perf.reason}</p>
          ) : (
            <LineChart
              ariaLabel="Time-weighted unit value of the book against the benchmark"
              className="h-72"
              series={[
                { name: "Your book", data: unit },
                ...(bench.length ? [{ name: `${perf?.benchmark ?? "Benchmark"} (EUR)`, data: bench, color: "#94a3b8" }] : []),
              ]}
              yLog={logScale}
              yName={logScale ? "Unit value (log scale)" : "Unit value"}
              yFormat={(v) => formatNumber(v, { digits: 0 })}
            />
          )}
        </section>

        <div className="grid grid-cols-1 gap-3 xl:grid-cols-[1.4fr_1fr]">
          <section className="rounded-md p-3 bg-surface">
            <h2 className="mb-2 text-sm font-semibold text-text-primary">Drawdown from the previous high</h2>
            <AreaChart
              ariaLabel="Drawdown of the book's unit value"
              tone="danger"
              className="h-56"
              series={[{ name: "Drawdown", data: drawdown }]}
              yFormat={(v) => `${Math.round(v * 100)} %`}
            />
          </section>
          <section className="rounded-md p-3 bg-surface">
            <h2 className="mb-2 text-sm font-semibold text-text-primary">Allocation (EUR)</h2>
            <DonutChart
              ariaLabel="Portfolio allocation"
              className="h-56"
              slices={Object.entries(summary.data?.weights ?? {}).map(([name, value]) => ({ name, value: Number(value) }))}
            />
          </section>
        </div>

        <div className="rounded-md p-3 text-xs text-text-secondary bg-surface">
          {perf?.available
            ? `${perf.method} ${perf.days} days from the first position snapshot. Returns over less than a year are not annualised.`
            : "The book's return starts with its first position snapshot, taken after every sync."}
        </div>
      </div>
    </div>
  );
}
