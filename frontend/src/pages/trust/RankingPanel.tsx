import type { TrustRankingSection } from "../../lib/api";
import { Card } from "../../components/ui/card";
import { Skeleton } from "../../components/ui/skeleton";
import { fmtDay, fmtPct, fmtPp } from "./trustFormat";
import { useTrustRanking } from "./useTrust";

const TIER_LABEL = { top: "Top third", middle: "Middle third", bottom: "Bottom third" } as const;

const STAGE_LABEL: Record<string, string> = {
  history_ingest: "No usable price history",
  momentum_quality: "Momentum / quality screen",
  quant_signals: "Quant signals",
  sentiment_fundamentals: "Sentiment / fundamentals",
  portfolio_fit: "Portfolio fit",
  backtest_vs_benchmark: "Backtest vs benchmark",
  alpha_screener: "Alpha screener",
  alpha_miner: "Alpha miner",
  verification_gate: "Verification gate",
  pipeline: "Missing from the pipeline",
};

function stageLabel(stage: string): string {
  return STAGE_LABEL[stage] ?? stage.replace(/_/g, " ");
}

function fmtIc(v: number | null | undefined): string {
  return v == null ? "—" : v.toFixed(3);
}

function interval(ci: [number, number] | null | undefined, digits = 1): string {
  return ci ? ` (90 %: ${fmtPp(ci[0], digits)} to ${fmtPp(ci[1], digits)})` : "";
}

function Section({ data, exploratory = false }: { data: TrustRankingSection; exploratory?: boolean }) {
  const ic = data.ic;
  const tiers = data.tiers;
  return (
    <div className={exploratory ? "space-y-3 opacity-75" : "space-y-3"} data-testid={exploratory ? "ranking-exploratory" : "ranking-live"}>
      <p className="text-xs text-text-muted">
        {data.n_runs} runs ({data.n_cohort_runs} with at least 30 scored stocks), {data.n_snapshots.toLocaleString("en-US")} scored
        names, {data.n_outcomes.toLocaleString("en-US")} measured outcomes
        {data.first_issue ? `, ${fmtDay(data.first_issue)} to ${fmtDay(data.last_issue)}` : ""}.
      </p>
      {ic.n_runs === 0 ? (
        <p className="text-sm text-text-secondary">
          No run has a 21-day outcome yet. Each Saturday the ledger measures every scored stock once its 5, 10, 21 and 63
          trading days have passed.
        </p>
      ) : (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
          <div>
            <dt className="text-xs text-text-muted">Rank IC (21 days)</dt>
            <dd className="font-mono text-sm font-semibold tabular-nums">{fmtIc(ic.mean)}</dd>
            <dd className="text-[11px] text-text-muted">
              {ic.n_runs} runs{ic.t_nw != null ? ` · Newey-West t ${ic.t_nw.toFixed(1)}` : ""}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Runs with IC above 0</dt>
            <dd className="font-mono text-sm font-semibold tabular-nums">{fmtPct(ic.share_positive)}</dd>
            <dd className="text-[11px] text-text-muted">{ic.icir != null ? `ICIR ${ic.icir.toFixed(2)}` : ""}</dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Within sectors</dt>
            <dd className="font-mono text-sm font-semibold tabular-nums">{fmtIc(data.sector_neutral_ic.mean)}</dd>
            <dd className="text-[11px] text-text-muted">sector-neutral IC</dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Picks vs the other scored stocks</dt>
            <dd className="font-mono text-sm font-semibold tabular-nums">{fmtPp(data.picked_vs_rest.mean_gap)}</dd>
            <dd className="text-[11px] text-text-muted">per 21 days{interval(data.picked_vs_rest.ci)}</dd>
          </div>
        </dl>
      )}

      {ic.n_runs > 0 ? (
        <div className="grid gap-3 md:grid-cols-2">
          <table className="w-full text-xs">
            <caption className="mb-1 text-left text-xs font-medium text-text-primary">Does the signal fade? IC by horizon</caption>
            <thead>
              <tr className="border-b border-border text-left text-text-muted">
                <th className="py-1 pr-3 font-normal">Trading days</th>
                <th className="py-1 pr-3 font-normal">Mean IC</th>
                <th className="py-1 text-right font-normal">Runs</th>
              </tr>
            </thead>
            <tbody>
              {data.ic_decay.map((d) => (
                <tr key={d.horizon_days} className="border-b border-border/50 last:border-0">
                  <td className="py-1 pr-3">{d.horizon_days}</td>
                  <td className="py-1 pr-3 font-mono tabular-nums">{fmtIc(d.mean_ic)}</td>
                  <td className="py-1 text-right font-mono tabular-nums">{d.n_runs}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <table className="w-full text-xs">
            <caption className="mb-1 text-left text-xs font-medium text-text-primary">
              Mean excess vs your ETF by score fifth (21 days)
            </caption>
            <thead>
              <tr className="border-b border-border text-left text-text-muted">
                <th className="py-1 pr-3 font-normal">Fifth</th>
                <th className="py-1 text-right font-normal">Mean excess</th>
              </tr>
            </thead>
            <tbody>
              {data.quintiles.map((q) => (
                <tr key={q.quintile} className="border-b border-border/50 last:border-0">
                  <td className="py-1 pr-3">{q.quintile === 1 ? "1 (highest scores)" : q.quintile === 5 ? "5 (lowest)" : q.quintile}</td>
                  <td className="py-1 text-right font-mono tabular-nums">{fmtPp(q.mean_excess)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {tiers && tiers.n_eff > 0 ? (
        <div className="space-y-1" data-testid="ranking-tiers">
          <p className="text-xs font-medium text-text-primary">How often each score tier beat your ETF over 21 days</p>
          <ul className="space-y-0.5 text-xs text-text-secondary">
            {tiers.tiers.map((t) => (
              <li key={t.tier}>
                {TIER_LABEL[t.tier]}: <span className="font-mono">{fmtPct(t.hit_rate)}</span>
                {t.range ? ` (90 %: ${fmtPct(t.range[0])} to ${fmtPct(t.range[1])})` : ""}
              </li>
            ))}
            <li>
              Every scored stock (base rate): <span className="font-mono">{fmtPct(tiers.base.hit_rate)}</span>
            </li>
          </ul>
          <p className="text-[11px] text-text-muted">
            Based on {tiers.n_eff} issue dates: names scored on one date share one market move, so the range counts dates,
            not names.
          </p>
        </div>
      ) : null}

      {data.gate_check.length > 0 ? (
        <table className="w-full text-xs" data-testid="ranking-gates">
          <caption className="mb-1 text-left text-xs font-medium text-text-primary">
            Do the filters remove losers? Rejected names minus scored ones, same run, 21 days
          </caption>
          <thead>
            <tr className="border-b border-border text-left text-text-muted">
              <th className="py-1 pr-3 font-normal">Filter</th>
              <th className="py-1 pr-3 font-normal">Gap</th>
              <th className="py-1 text-right font-normal">Names (runs)</th>
            </tr>
          </thead>
          <tbody>
            {data.gate_check.map((g) => (
              <tr key={g.reject_stage} className="border-b border-border/50 last:border-0">
                <td className="py-1 pr-3">{stageLabel(g.reject_stage)}</td>
                <td className="py-1 pr-3 font-mono tabular-nums">
                  {fmtPp(g.mean_gap)}
                  {interval(g.ci)}
                </td>
                <td className="py-1 text-right font-mono tabular-nums">
                  {g.n_names} ({g.n_runs})
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {data.etf_ic.n_runs > 0 ? (
        <p className="text-xs text-text-secondary">
          ETFs, scored on their own (never pooled with stocks): mean rank IC {fmtIc(data.etf_ic.mean)} over {data.etf_ic.n_runs} runs.
        </p>
      ) : null}
    </div>
  );
}

/** How well Discover's score sorted every stock it scored: descriptive, never a verdict (that is F2). */
export function RankingPanel() {
  const query = useTrustRanking();
  if (query.isLoading) return <Skeleton className="h-40 w-full rounded-md" />;
  if (query.isError || !query.data) {
    return (
      <Card className="p-4 text-sm text-text-secondary" role="status">
        The ranking numbers are unavailable right now.
      </Card>
    );
  }
  const data = query.data;
  return (
    <section aria-labelledby="trust-ranking" className="space-y-3">
      <div>
        <h2 id="trust-ranking" className="text-sm font-semibold text-text-primary">
          How well the score sorts stocks
        </h2>
        <p className="text-xs text-text-secondary">
          Every stock Discover scores is recorded, not only the picks, and measured against your ETF afterwards. {data.verdict_note}
        </p>
      </div>
      <Card className="space-y-3 p-4">
        <Section data={data.live} />
      </Card>
      {data.exploratory ? (
        <Card className="space-y-2 border-dashed p-4">
          <h3 className="text-xs font-semibold text-text-secondary">Exploratory: rebuilt from runs before the ledger existed</h3>
          <p className="text-[11px] text-text-muted">
            Never part of a verdict. The scoring formula changed between those runs, scores were rewritten for shortlisted
            names, and which names were scored is reconstructed from the filters that rejected them.
          </p>
          <Section data={data.exploratory} exploratory />
        </Card>
      ) : null}
    </section>
  );
}
