import { useState } from "react";
import { Card } from "../../components/ui/card";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { LineChart } from "../../components/charts/LineChart";
import { AreaChart } from "../../components/charts/AreaChart";
import { formatCurrency, formatNumber, formatPercent } from "../../lib/format";
import { useBookPerformance } from "../quantlab/hooks/useBookPerformance";
import { drawdownSeries, monthlyReturns, periodReturn, sliceSeries, type Period } from "./performanceMath";
import { token } from "../../lib/theme/tokens";
import { Table } from "../../components/ui/table";

const PERIODS: { key: Period; label: string }[] = [
  { key: "all", label: "Since start" },
  { key: "ytd", label: "This year" },
  { key: "1y", label: "12 months" },
];

const pct = (v: number | null | undefined) => formatPercent(v, { digits: 1 });
const pp = (v: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${formatNumber(v * 100, { digits: 1 })} pp`);
const tone = (v: number | null | undefined) => (v == null ? "" : v >= 0 ? "text-success" : "text-danger");

function Stat({ label, value, sub, className = "" }: { label: string; value: string; sub?: string; className?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className={`font-mono text-base font-semibold tabular-nums ${className}`}>{value}</dd>
      {sub ? <dd className="text-[11px] text-text-muted">{sub}</dd> : null}
    </div>
  );
}

/**
 * The real book's return across every broker, in EUR: time-weighted unit
 * value (what the investments earned), the money-weighted IRR (what your
 * deposits earned, timing included) and MSCI World over the same days.
 */
export function PerformanceTab() {
  const book = useBookPerformance();
  const [period, setPeriod] = useState<Period>("all");

  if (book.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-28 w-full rounded-md" />
        <Skeleton className="h-64 w-full rounded-md" />
      </div>
    );
  }
  if (book.isError || !book.data) {
    return (
      <Card className="flex items-center justify-between gap-3 p-4">
        <p className="text-sm text-danger">Performance could not be loaded.</p>
        <Button variant="outline" size="sm" onClick={() => book.refetch()}>
          Retry
        </Button>
      </Card>
    );
  }
  const perf = book.data;
  if (!perf.available) {
    return (
      <Card className="p-4 text-sm text-text-secondary" data-testid="performance-unavailable">
        {perf.reason ?? "Not enough position history yet."}
      </Card>
    );
  }

  const series = sliceSeries(perf.series, period);
  const bookReturn = periodReturn(series.map((p) => p.unit_value));
  const benchReturn = periodReturn(series.map((p) => p.benchmark));
  const excess = bookReturn != null && benchReturn != null ? bookReturn - benchReturn : null;
  const dd = drawdownSeries(series);
  const maxDd = dd.reduce((m, [, v]) => Math.min(m, v), 0);
  const flows = series.slice(1).reduce((sum, p) => sum + p.net_flow, 0);
  const months = monthlyReturns(series);
  const benchName = `${perf.benchmark ?? "MSCI World"} (EUR)`;
  const whole = period === "all";

  return (
    <div className="space-y-4">
      <div role="group" aria-label="Period" className="flex w-fit flex-wrap items-center gap-1 rounded-md border border-border p-0.5">
        {PERIODS.map((p) => (
          <Button
            key={p.key}
            type="button"
            size="sm"
            variant={period === p.key ? "default" : "ghost"}
            aria-pressed={period === p.key}
            onClick={() => setPeriod(p.key)}
          >
            {p.label}
          </Button>
        ))}
      </div>

      <Card className="p-4">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-5" data-testid="performance-stats">
          <Stat
            label="Your investments (time-weighted)"
            value={pct(whole && perf.twr_annualised != null ? perf.twr_annualised : bookReturn)}
            sub={whole && perf.twr_annualised != null ? "a year, annualised" : `${series[0]?.date ?? ""} – ${perf.end}`}
            className={tone(bookReturn)}
          />
          <Stat
            label={benchName}
            value={pct(whole && perf.benchmark_annualised != null ? perf.benchmark_annualised : benchReturn)}
            sub="same days"
          />
          <Stat
            label="Difference"
            value={pp(excess)}
            sub="before taxes, after fees"
            className={tone(excess)}
          />
          <Stat
            label="Your deposits earned (IRR)"
            value={whole ? pct(perf.irr_annualised ?? perf.irr_period) : "—"}
            sub={whole ? (perf.irr_annualised != null ? "a year, timing included" : "over the period, timing included") : "whole period only"}
          />
          <Stat label="Deepest fall" value={pct(maxDd)} sub="from a previous high" className={maxDd < 0 ? "text-danger" : ""} />
        </dl>
        <p className="mt-3 text-xs text-text-secondary">
          Net deposits in the period {formatCurrency(flows, "EUR")} · value now {formatCurrency(perf.value_end_eur ?? 0, "EUR")}.
        </p>
      </Card>

      <section className="rounded-md bg-surface p-3">
        <h2 className="mb-2 text-sm font-semibold text-text-primary">Growth of 1 € invested, against {benchName}</h2>
        <LineChart
          ariaLabel="Time-weighted unit value of the book against the benchmark"
          className="h-64"
          xType="time"
          series={[
            { name: "Your book", data: series.map((p) => [p.date, p.unit_value / (series[0]?.unit_value || 1)] as [string, number]) },
            ...(series[0]?.benchmark
              ? [{
                  name: benchName,
                  color: token("text-muted"),
                  data: series
                    .filter((p) => p.benchmark != null)
                    .map((p) => [p.date, (p.benchmark as number) / (series[0].benchmark as number)] as [string, number]),
                }]
              : []),
          ]}
          yFormat={(v) => formatNumber(v, { digits: 2 })}
        />
      </section>

      <section className="rounded-md bg-surface p-3">
        <h2 className="mb-2 text-sm font-semibold text-text-primary">Fall from the previous high</h2>
        <AreaChart
          ariaLabel="Drawdown of the book's unit value"
          tone="danger"
          className="h-40"
          series={[{ name: "Drawdown", data: dd }]}
          yFormat={(v) => `${Math.round(v * 100)} %`}
        />
      </section>

      {months.length > 0 && (
        <section className="rounded-md bg-surface p-3">
          <h2 className="mb-2 text-sm font-semibold text-text-primary">Month by month</h2>
          <div className="overflow-x-auto">
            <Table className="w-full text-sm" data-testid="performance-months">
              <thead>
                <tr className="border-b border-border text-left text-xs text-text-muted">
                  <th className="py-2 pr-3 font-normal">Month</th>
                  <th className="py-2 pr-3 text-right font-normal">Your book</th>
                  <th className="py-2 pr-3 text-right font-normal">{perf.benchmark ?? "Benchmark"}</th>
                  <th className="py-2 pr-3 text-right font-normal">Difference</th>
                  <th className="py-2 text-right font-normal">Net deposits</th>
                </tr>
              </thead>
              <tbody>
                {months.map((m) => {
                  const diff = m.book != null && m.benchmark != null ? m.book - m.benchmark : null;
                  return (
                    <tr key={m.month} className="border-b border-border/50 last:border-0">
                      <td className="py-1.5 pr-3 font-mono text-text-secondary">
                        {m.month}
                        {m.partial ? <span className="ml-1 text-[10px] text-text-muted">part</span> : null}
                      </td>
                      <td className={`py-1.5 pr-3 text-right font-mono tabular-nums ${tone(m.book)}`}>{pct(m.book)}</td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums text-text-secondary">{pct(m.benchmark)}</td>
                      <td className={`py-1.5 pr-3 text-right font-mono tabular-nums ${tone(diff)}`}>
                        {pp(diff)}
                      </td>
                      <td className="py-1.5 text-right font-mono tabular-nums text-text-secondary">
                        {formatCurrency(m.netFlow, "EUR")}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          </div>
        </section>
      )}

      <p className="text-[11px] text-text-muted">
        Securities at every connected broker (DKB, Scalable, Trading 212), in EUR; cash is not included. The
        time-weighted return removes the effect of deposits and withdrawals, so it compares with {benchName};
        the IRR is what your own money earned, timing included. Under a year nothing is annualised.
        {(perf.unpriced?.length ?? 0) > 0 ? ` No price for ${perf.unpriced!.join(", ")}; left out.` : ""}
        {perf.days_without_full_prices ? ` ${perf.days_without_full_prices} day(s) lacked a full set of prices.` : ""}{" "}
        Estimate · not financial advice.
      </p>
    </div>
  );
}
