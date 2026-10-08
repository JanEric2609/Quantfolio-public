import { History } from "lucide-react";
import type { TrustHistory, TrustTiltCard } from "../../lib/api";
import { Badge } from "../../components/ui/badge";
import { Card } from "../../components/ui/card";
import { Skeleton } from "../../components/ui/skeleton";
import { fmtDay, fmtPct } from "./trustFormat";
import { useTrustHistory } from "./useTrust";

function pctYear(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}${Math.abs(v * 100).toFixed(1)} % a year`;
}

function StatusChip({ passed, label }: { passed: boolean | null; label: string }) {
  return (
    <Badge variant={passed == null ? "secondary" : passed ? "success" : "danger"} className="text-xs">
      {label}
    </Badge>
  );
}

/** Full period | last ten years | after the haircut, on one scale. */
function DecayBars({ card }: { card: TrustTiltCard }) {
  const rows: [string, number | null][] = [
    ["Full period", card.decay.full_period],
    ["Last 10 years", card.decay.last_10_years],
    ["After the haircut, costs and tax", card.decay.after_haircut],
  ];
  const max = Math.max(0.0001, ...rows.map(([, v]) => Math.abs(v ?? 0)));
  return (
    <div className="space-y-1" role="list" aria-label={`${card.label}: decay of the premium`}>
      {rows.map(([label, v]) => (
        <div key={label} role="listitem" className="grid grid-cols-[9rem_1fr_6rem] items-center gap-2 text-xs">
          <span className="text-text-muted">{label}</span>
          <span className="h-2 rounded bg-surface-2">
            <span
              className={`block h-2 rounded ${v != null && v < 0 ? "bg-danger" : "bg-primary"}`}
              style={{ width: `${Math.round((Math.abs(v ?? 0) / max) * 100)}%` }}
            />
          </span>
          <span className="text-right font-mono tabular-nums">{pctYear(v)}</span>
        </div>
      ))}
    </div>
  );
}

function TiltCard({ card }: { card: TrustTiltCard }) {
  const fan = card.fan;
  return (
    <Card className="space-y-3 p-4" data-testid={`history-tilt-${card.region}-${card.strategy}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h4 className="text-sm font-semibold text-text-primary">
            {card.label} · {card.region === "world" ? "World" : card.region === "europe" ? "Europe" : card.region}
          </h4>
          <p className="text-[11px] text-text-muted">
            {card.gates_tilt ? "Decides the plan's tilt." : "Shown for comparison; does not decide the tilt."} {card.citation}
          </p>
        </div>
        <StatusChip
          passed={card.passed}
          label={`${card.passed ? "Passed our tests" : "Did not pass"}${card.long_short_t != null ? ` · t = ${card.long_short_t.toFixed(2)} vs bar ${card.t_bar.toFixed(1)}` : ""}`}
        />
      </div>
      <DecayBars card={card} />
      {fan ? (
        <p className="text-xs text-text-secondary">
          Over {fan.years} years against your ETF, after costs and tax: in the middle half of simulated periods{" "}
          {fmtPct(fan.percentiles.find((p) => p.p === 25)?.cumulative_excess ?? null, 1)} to{" "}
          {fmtPct(fan.percentiles.find((p) => p.p === 75)?.cumulative_excess ?? null, 1)}; in the worst 5 %{" "}
          {fmtPct(fan.percentiles.find((p) => p.p === 5)?.cumulative_excess ?? null, 1)} or less. It came out ahead in about{" "}
          {Math.round(fan.share_beat * 100)} of 100 periods.
        </p>
      ) : null}
      <p className={`text-[11px] ${card.stale ? "text-warning" : "text-text-muted"}`}>
        {card.months ? `${card.months} months of data` : "Data"} {card.start ? `from ${fmtDay(card.start)} ` : ""}to {fmtDay(card.end)}
        {card.stale ? " — older than 12 months: re-run the factor study on a fresh data export." : "."}
      </p>
    </Card>
  );
}

function Panel({ data }: { data: TrustHistory }) {
  const rm = data.ranking_model;
  const sat = data.satellite;
  const best = rm.models.reduce<(typeof rm.models)[number] | null>(
    (acc, m) => (acc == null || (m.ic_t ?? 0) > (acc.ic_t ?? 0) ? m : acc),
    null,
  );
  return (
    <div className="space-y-3">
      <p className="text-xs text-text-secondary">
        What decades of past data say about the factor tilt and the stock-ranking research, after costs, tax and a haircut
        for overfitting. We plan on {Math.round((1 - data.haircut.publication_decay) * 100)} % of the historical edge:
        published effects lost about {Math.round(data.haircut.publication_decay * 100)} % after publication (McLean and
        Pontiff 2016). {data.n_trials != null ? `${data.n_trials.toLocaleString("en-US")} ideas have been tried and recorded; results are judged against that many. ` : ""}
        It says nothing about the live tests above.
      </p>

      <h3 className="text-xs font-semibold uppercase tracking-wide text-text-muted">Factor tilt</h3>
      {data.tilt_cards.length > 0 ? (
        <div className="grid gap-3 lg:grid-cols-2">
          {data.tilt_cards.map((c) => (
            <TiltCard key={`${c.region}-${c.strategy}`} card={c} />
          ))}
        </div>
      ) : (
        <p className="text-sm text-text-secondary">The factor study has not been run yet.</p>
      )}
      <p className="text-[11px] text-text-muted" data-testid="history-tilt-review">
        Live check: {data.tilt_review.note}
      </p>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card className="space-y-2 p-4" data-testid="history-ranking-model">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <h3 className="text-sm font-semibold text-text-primary">Stock-ranking model</h3>
            <StatusChip passed={null} label="Strong ranking evidence · not live, gates no money" />
          </div>
          {best ? (
            <>
              <p className="text-sm text-text-secondary">
                Across {best.months} out-of-sample months it ranked stocks better than chance (rank IC{" "}
                {best.ic_mean?.toFixed(3) ?? "—"}
                {best.ic_t != null ? `, t ≈ ${best.ic_t.toFixed(0)}` : ""}). Long-only it earned {pctYear(best.long_only_annual)} over
                the market before costs; in the last {Math.round((best.recent_months ?? 120) / 12)} years the rank IC was{" "}
                {best.recent_ic_mean?.toFixed(3) ?? "—"}.
              </p>
              <p className="text-[11px] text-text-muted">
                Data to {fmtDay(rm.data_to)}{rm.stale ? " (older than 12 months)" : ""}. Pseudo out-of-sample: walk-forward
                with a purge and embargo.
              </p>
            </>
          ) : (
            <p className="text-sm text-text-secondary">No stored model record yet (the weekly evidence gate stores it).</p>
          )}
        </Card>
        <Card className="space-y-2 p-4" data-testid="history-satellite">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <h3 className="text-sm font-semibold text-text-primary">Stock-picking satellite</h3>
            {sat.available ? (
              <StatusChip passed={sat.unlocked} label={sat.unlocked ? "Passed: unlocked" : "Did not pass: stays locked"} />
            ) : (
              <StatusChip passed={null} label="Not graded yet" />
            )}
          </div>
          <p className="text-sm text-text-secondary">
            Turning those rankings into a handful of stocks you can actually hold, after fees and German tax.
            {sat.best?.dsr != null
              ? ` Best mined score: Deflated Sharpe ${sat.best.dsr.toFixed(2)} against a bar of ${sat.dsr_bar}.`
              : ""}
            {!sat.unlocked && sat.available ? " That is why stock picks stay locked." : ""}
          </p>
          {sat.reason ? <p className="text-[11px] text-text-muted">{sat.reason}</p> : null}
        </Card>
      </div>

      <Card className="p-4" data-testid="history-not-covered">
        <h3 className="text-sm font-semibold text-text-primary">Not covered</h3>
        <p className="text-sm text-text-secondary">{data.not_covered}</p>
      </Card>
      <ul className="space-y-0.5 text-[11px] text-text-muted">
        {data.disclosures.map((d) => (
          <li key={d}>{d}</li>
        ))}
      </ul>
    </div>
  );
}

/** "Historical evidence (simulated, not live)": below the live tests, never mixed into them. */
export function HistoryPanel() {
  const query = useTrustHistory();
  return (
    <section aria-labelledby="trust-history" className="space-y-3 rounded-lg border border-dashed border-border p-4">
      <h2 id="trust-history" className="flex items-center gap-2 text-sm font-semibold text-text-primary">
        <History className="h-4 w-4 text-text-muted" aria-hidden="true" />
        {query.data?.title ?? "Historical evidence (simulated, not live)"}
      </h2>
      {query.isLoading ? (
        <Skeleton className="h-40 w-full rounded-md" />
      ) : query.isError || !query.data ? (
        <p className="text-sm text-text-secondary" role="status">
          The historical record is unavailable right now.
        </p>
      ) : (
        <Panel data={query.data} />
      )}
    </section>
  );
}
