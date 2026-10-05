import type { LedgerBenchmark } from "../../../lib/api";

const pct = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(2)} %`);
const pp = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v * 100).toFixed(2)} pp`;

/**
 * Compact "vs benchmark": the ledger's time-weighted return beside the passive
 * core ETF over the same window. A window under a year is a snapshot of a short
 * stretch, and the card says so instead of ranking the portfolio on it.
 */
export function BenchmarkCard({ benchmark }: { benchmark: LedgerBenchmark }) {
  const tone = benchmark.excess == null ? "text-text-primary" : benchmark.excess >= 0 ? "text-success" : "text-danger";
  return (
    <section className="rounded-md border border-line bg-panel p-4" aria-labelledby="vs-benchmark">
      <h3 id="vs-benchmark" className="mb-3 text-sm font-semibold">
        vs benchmark ({benchmark.benchmark_symbol})
      </h3>
      <dl className="grid grid-cols-3 gap-4">
        <div>
          <dt className="text-xs text-text-muted">Your TWR</dt>
          <dd className="font-mono text-sm font-semibold tabular-nums text-text-primary">{pct(benchmark.portfolio_twr)}</dd>
        </div>
        <div>
          <dt className="text-xs text-text-muted">{benchmark.benchmark_symbol}</dt>
          <dd className="font-mono text-sm font-semibold tabular-nums text-text-primary">{pct(benchmark.benchmark_return)}</dd>
        </div>
        <div>
          <dt className="text-xs text-text-muted">Difference</dt>
          <dd className={`font-mono text-sm font-semibold tabular-nums ${tone}`}>{pp(benchmark.excess)}</dd>
        </div>
      </dl>
      <p className="mt-3 text-xs text-text-muted">
        {benchmark.message ??
          `${benchmark.period_start} to ${benchmark.period_end} (${benchmark.period_days} days).`}
        {benchmark.short_window && benchmark.message == null
          ? " Under a year: a snapshot of a short stretch, not a track record."
          : ""}
      </p>
    </section>
  );
}
