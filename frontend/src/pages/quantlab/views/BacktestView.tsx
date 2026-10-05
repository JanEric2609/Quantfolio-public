import { useMemo, useState } from "react";
import { formatCurrency, formatNumber, formatPercent } from "../../../lib/format";
import { LineChart } from "../../../components/charts/LineChart";
import { Button } from "../../../components/ui/button";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";
import { useBacktest, useBacktestStrategies } from "../hooks/useBacktest";
import type { BacktestLineStats, BacktestRun, BacktestRunRequest } from "../../../lib/api";

const pct = (v?: number | null, digits = 1) => formatPercent(v, { digits });
const eur = (v?: number | null) => (v == null ? "-" : formatCurrency(v, "EUR", { digits: 0 }));
const num = (v?: number | null, digits = 2) => formatNumber(v, { digits });

const FUND_CLASSES: { value: NonNullable<BacktestRunRequest["fund_class"]>; label: string }[] = [
  { value: "aktien", label: "Equity fund (30 % exempt)" },
  { value: "misch", label: "Mixed fund (15 % exempt)" },
  { value: "immobilien", label: "Real-estate fund (60 % exempt)" },
  { value: "other", label: "Stock, bond fund or unknown (0 %)" },
];

const VERDICT: Record<string, { title: string; tone: string }> = {
  evidence: { title: "Evidence of timing skill", tone: "border-success/40 bg-success/10" },
  insufficient_evidence: { title: "Insufficient evidence", tone: "border-warn/40 bg-warn/10" },
  too_short: { title: "Too short to judge", tone: "border-warn/40 bg-warn/10" },
  reference: { title: "Reference", tone: "border-line bg-surface" },
};

const inputCls = "mt-1 w-full rounded border border-line bg-bg px-2 py-1";

/**
 * One timing rule on one instrument, in EUR, traded a day after its signal,
 * after costs and German tax, next to holding the same instrument and the
 * benchmark. Every run is a trial: the verdict deflates the excess over
 * holding for all trials so far (DSR) and checks the rule's parameter grid
 * for overfitting (PBO).
 */
export function BacktestView() {
  const strategies = useBacktestStrategies();
  const backtest = useBacktest();
  const [ticker, setTicker] = useState("EUNL.DE");
  const [strategy, setStrategy] = useState("trend");
  const [edited, setEdited] = useState<Record<string, Record<string, string>>>({});
  const [years, setYears] = useState(10);
  const [commission, setCommission] = useState("10");
  const [spread, setSpread] = useState("5");
  const [fundClass, setFundClass] = useState<NonNullable<BacktestRunRequest["fund_class"]>>("aktien");
  const [tax, setTax] = useState(true);

  const selected = strategies.data?.find((s) => s.key === strategy);
  // Each rule keeps its own edits; untouched fields show its defaults.
  const params: Record<string, string> = {
    ...Object.fromEntries(Object.entries(selected?.defaults ?? {}).map(([k, v]) => [k, String(v)])),
    ...(edited[strategy] ?? {}),
  };
  const setParams = (next: Record<string, string>) => setEdited({ ...edited, [strategy]: next });

  const run = (event: React.FormEvent) => {
    event.preventDefault();
    backtest.mutate({
      ticker: ticker.trim().toUpperCase(),
      strategy,
      params: Object.fromEntries(Object.entries(params).map(([k, v]) => [k, Number(v.replace(",", "."))])),
      years,
      commission_bps: Number(commission.replace(",", ".")) || 0,
      spread_bps: Number(spread.replace(",", ".")) || 0,
      fund_class: fundClass,
      apply_tax: tax,
    });
  };

  return (
    <div className="flex flex-col gap-4 xl:flex-row">
      <section className="w-full shrink-0 rounded-md p-4 xl:sticky xl:top-20 xl:w-[32%] bg-surface">
        <h2 className="mb-3 text-sm font-semibold text-text-primary">Rule</h2>
        <form onSubmit={run} className="space-y-3 text-xs">
          <label className="block">
            <span className="text-text-muted">Ticker</span>
            <input value={ticker} onChange={(e) => setTicker(e.target.value)} className={`${inputCls} uppercase`} />
          </label>
          <label className="block">
            <span className="text-text-muted">Strategy</span>
            <select value={strategy} onChange={(e) => setStrategy(e.target.value)} className={inputCls}>
              {(strategies.data ?? []).map((s) => (
                <option key={s.key} value={s.key}>{s.label}</option>
              ))}
            </select>
            {selected && <span className="mt-1 block text-[11px] text-text-muted">{selected.about}</span>}
          </label>
          {Object.keys(params).length > 0 && (
            <div className="grid grid-cols-3 gap-2">
              {Object.entries(params).map(([k, v]) => (
                <label key={k} className="block">
                  <span className="text-text-muted">{k}</span>
                  <input inputMode="decimal" value={v} onChange={(e) => setParams({ ...params, [k]: e.target.value })} className={inputCls} />
                </label>
              ))}
            </div>
          )}
          <div className="grid grid-cols-3 gap-2">
            <label className="block">
              <span className="text-text-muted">Years</span>
              <select value={years} onChange={(e) => setYears(Number(e.target.value))} className={inputCls}>
                {[5, 10, 15, 20].map((y) => <option key={y} value={y}>{y}</option>)}
              </select>
            </label>
            <label className="block">
              <span className="text-text-muted">Commission (bp)</span>
              <input inputMode="decimal" value={commission} onChange={(e) => setCommission(e.target.value)} className={inputCls} />
            </label>
            <label className="block">
              <span className="text-text-muted">Half-spread (bp)</span>
              <input inputMode="decimal" value={spread} onChange={(e) => setSpread(e.target.value)} className={inputCls} />
            </label>
          </div>
          <label className="block">
            <span className="text-text-muted">Fund type for tax</span>
            <select value={fundClass} onChange={(e) => setFundClass(e.target.value as typeof fundClass)} className={inputCls}>
              {FUND_CLASSES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={tax} onChange={(e) => setTax(e.target.checked)} />
            <span>Tax on sales</span>
          </label>
          <Button className="w-fit" disabled={backtest.isPending}>{backtest.isPending ? "Running…" : "Run backtest"}</Button>
          <p className="text-[11px] text-text-muted">
            Every run counts as a trial. The more rules and settings you try, the higher the bar for any of them.
          </p>
        </form>
      </section>

      <div className="min-w-0 flex-1 space-y-4">
        {backtest.error && (
          <div className="rounded-md border border-danger/30 bg-danger/10 p-3 text-sm">
            {backtest.error instanceof Error ? backtest.error.message : "The backtest failed."}
          </div>
        )}
        {backtest.data && !backtest.data.available && (
          <div className="rounded-md p-3 text-sm text-text-secondary bg-surface">{backtest.data.reason}</div>
        )}
        {backtest.data?.available && <BacktestResultPanel r={backtest.data} />}
        {!backtest.data && !backtest.error && (
          <div className="rounded-md p-3 text-sm text-text-secondary bg-surface">
            Pick a rule and run it. The result shows the rule next to simply holding the same instrument and next to
            the benchmark, and says whether the difference is more than luck.
          </div>
        )}
      </div>
    </div>
  );
}

function BacktestResultPanel({ r }: { r: BacktestRun }) {
  const s = r.stats!;
  const ev = r.evidence!;
  const v = VERDICT[ev.verdict];
  const series = r.series ?? [];
  const lines = useMemo(() => {
    const out = [
      { name: r.label, data: series.map((p) => [p.date, p.strategy] as [string, number]) },
      { name: `Hold ${r.ticker}`, data: series.map((p) => [p.date, p.buy_hold] as [string, number]) },
    ];
    if (r.benchmark) out.push({ name: r.benchmark, data: series.filter((p) => p.benchmark != null).map((p) => [p.date, p.benchmark!] as [string, number]) });
    return out;
  }, [series, r.label, r.ticker, r.benchmark]);

  const metrics: MetricItem[] = [
    { label: "Rule, a year", value: pct(s.strategy.cagr), hint: `holding: ${pct(s.buy_hold.cagr)}` },
    { label: "Worst fall", value: pct(s.strategy.max_drawdown), hint: `holding: ${pct(s.buy_hold.max_drawdown)}` },
    { label: "Invested", value: pct(s.strategy.exposure, 0), hint: `${s.strategy.trades ?? 0} trades` },
    { label: "Costs + tax", value: eur((s.strategy.costs_eur ?? 0) + (s.strategy.taxes_eur ?? 0)), hint: "on €10.000" },
  ];
  const rows: [string, BacktestLineStats | null][] = [
    [r.label, s.strategy],
    [`Hold ${r.ticker}`, s.buy_hold],
    [r.benchmark ?? "Benchmark", s.benchmark],
  ];

  return (
    <>
      <section className={`rounded-md border p-3 text-sm ${v.tone}`} data-testid="verdict">
        <h2 className="font-semibold">{v.title}</h2>
        <p className="mt-1 text-text-secondary">{ev.why}</p>
        <p className="mt-1 text-[11px] text-text-muted">
          {r.start} – {r.end} ({num(r.years, 1)} years after a {r.warmup_days}-day warm-up), prices in EUR.
        </p>
      </section>

      <SummaryStrip metrics={metrics} />

      <section className="rounded-md p-3 bg-surface">
        <h2 className="mb-1 text-sm font-semibold text-text-primary">Value of €100, after costs and tax</h2>
        <LineChart
          ariaLabel="Backtest: rule, holding and benchmark"
          className="h-80"
          series={lines}
          yLog
          xName="Date"
          yName="€ (log scale)"
          yFormat={(x) => formatNumber(x, { digits: 0 })}
        />
      </section>

      <section className="rounded-md p-3 bg-surface">
        <h2 className="mb-1 text-sm font-semibold text-text-primary">How far the rule fell from its high</h2>
        <LineChart
          ariaLabel="Backtest drawdown"
          className="h-40"
          area
          series={[{ name: "Drawdown", data: series.map((p) => [p.date, 100 * p.drawdown] as [string, number]), color: "#ef4444" }]}
          xName="Date"
          yName="% below the high"
          yFormat={(x) => `${Math.round(x)} %`}
        />
      </section>

      <section className="grid grid-cols-1 gap-2 md:grid-cols-2">
        <div className="rounded-md p-3 text-xs bg-surface space-y-1">
          <h2 className="mb-1 text-sm font-semibold text-text-primary">Is it more than luck?</h2>
          {ev.verdict === "reference" ? (
            <p className="text-text-secondary">Nothing to test: this is what the timing rules are measured against.</p>
          ) : (
            <>
              <p>
                Deflated Sharpe of the excess over holding: <b className="font-mono">{num(ev.dsr)}</b> at {ev.n_trials} trials
                ({num(ev.dsr_effective)} at {ev.n_trials_effective} distinct searches). Needs {ev.dsr_threshold}.
              </p>
              <p>
                Probability the best of {ev.pbo_grid_size} parameter settings is overfit:{" "}
                <b className="font-mono">{ev.pbo == null ? "-" : pct(ev.pbo, 0)}</b>. Above {pct(ev.pbo_threshold, 0)} the
                in-sample winner usually loses out of sample.
              </p>
              <p>
                Timing alpha against holding: {pct(r.vs_buy_hold?.alpha)} a year, t = {num(r.vs_buy_hold?.alpha_t_stat)}
                {" "}(Newey-West).
              </p>
              {ev.min_track_record_years != null ? (
                <p>Years of this excess needed to be 95 % sure it is above zero: {num(ev.min_track_record_years, 1)}.</p>
              ) : (
                <p>The excess is not above zero, so no track record would make it significant.</p>
              )}
            </>
          )}
          {r.vs_benchmark && (
            <p className="text-text-muted">
              Against {r.benchmark}: beta {num(r.vs_benchmark.beta)}, alpha {pct(r.vs_benchmark.alpha)} a year (t ={" "}
              {num(r.vs_benchmark.alpha_t_stat)}).
            </p>
          )}
        </div>
        <div className="overflow-x-auto rounded-md p-3 bg-surface">
          <table className="w-full text-xs">
            <thead className="text-text-muted">
              <tr>
                <th className="text-left font-normal" />
                <th className="text-right font-normal">Total</th>
                <th className="text-right font-normal">A year</th>
                <th className="text-right font-normal">Vol.</th>
                <th className="text-right font-normal">Sharpe</th>
                <th className="text-right font-normal">Worst fall</th>
              </tr>
            </thead>
            <tbody className="font-mono tabular-nums">
              {rows.map(([name, st]) => (
                <tr key={name} className="border-t border-line">
                  <td className="font-sans">{name}</td>
                  <td className="text-right">{pct(st?.total_return)}</td>
                  <td className="text-right">{pct(st?.cagr)}</td>
                  <td className="text-right">{pct(st?.volatility)}</td>
                  <td className="text-right">{num(st?.sharpe)}</td>
                  <td className="text-right">{pct(st?.max_drawdown)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[11px] text-text-muted">Sharpe in excess of {pct(r.risk_free)} cash.</p>
        </div>
      </section>

      {(r.trades?.length ?? 0) > 0 && (
        <details className="rounded-md p-3 text-xs bg-surface">
          <summary className="cursor-pointer text-sm font-semibold text-text-primary">Trades ({r.trades!.length})</summary>
          <table className="mt-2 w-full">
            <thead className="text-text-muted">
              <tr><th className="text-left font-normal">Date</th><th className="text-left font-normal">Side</th><th className="text-right font-normal">Price (EUR)</th></tr>
            </thead>
            <tbody>
              {r.trades!.map((t) => (
                <tr key={`${t.date}-${t.side}`} className="border-t border-line">
                  <td className="font-mono">{t.date}</td>
                  <td>{t.side}</td>
                  <td className="text-right font-mono tabular-nums">{num(t.price_eur)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      <section className="rounded-md p-3 text-xs text-text-secondary bg-surface space-y-1">
        <h2 className="text-sm font-semibold text-text-primary">Assumptions</h2>
        {Object.values(r.assumptions ?? {}).map((a) => <p key={a}>{a}</p>)}
        <p className="text-text-muted">A backtest, not a forecast or advice.</p>
      </section>
    </>
  );
}
