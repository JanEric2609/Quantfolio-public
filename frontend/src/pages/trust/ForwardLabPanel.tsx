import { FlaskConical } from "lucide-react";
import type { TrustForwardLab, TrustForwardLabSignal } from "../../lib/api";
import { Badge } from "../../components/ui/badge";
import { Skeleton } from "../../components/ui/skeleton";
import { fmtDay, fmtE } from "./trustFormat";
import { useTrustForwardLab } from "./useTrust";
import { formatNumber } from "../../lib/format";

const STATE: Record<TrustForwardLabSignal["state"], { label: string; variant: "secondary" | "success" | "danger" }> = {
  not_started: { label: "Not started", variant: "secondary" },
  collecting: { label: "Collecting", variant: "secondary" },
  evidence_of_skill: { label: "Works going forward", variant: "success" },
  evidence_of_harm: { label: "Works backwards", variant: "danger" },
};

const bp = (v: number | null | undefined) => (v == null ? "—" : `${formatNumber((v * 10000), { digits: 1 })} bp`);

function SignalRow({ s }: { s: TrustForwardLabSignal }) {
  const state = STATE[s.state];
  const r = s.robustness;
  return (
    <li className="space-y-0.5 border-t border-border pt-2 first:border-t-0 first:pt-0" data-testid={`forward-lab-${s.signal}`}>
      <div className="flex flex-wrap items-center gap-2 text-sm text-text-primary">
        <span className="font-medium">{s.label}</span>
        <Badge variant={state.variant} className="w-fit text-xs">
          {state.label}
        </Badge>
      </div>
      <p className="text-xs text-text-secondary">
        {s.days === 0
          ? "No day scored yet."
          : `${s.days} days on ${s.names} stocks · mean ${bp(s.mean_daily)} a day` +
            (s.ci90_daily ? ` (90 %: ${bp(s.ci90_daily[0])} to ${bp(s.ci90_daily[1])})` : "") +
            ` · evidence: works ${fmtE(s.e_skill)}, backwards ${fmtE(s.e_harm)}`}
      </p>
      {r && (
        <p className="text-xs text-text-muted">
          Stress checks: noise alone reached the bar in {r.false_alarm == null ? "—" : `${formatNumber((r.false_alarm * 100), { digits: 1 })} %`}{" "}
          of resamples; without its 5 best days {fmtE(r.without_best_days)}; first half {fmtE(r.first_half)}, second half{" "}
          {fmtE(r.second_half)}.
        </p>
      )}
      <p className="text-[11px] text-text-muted">{s.source}</p>
    </li>
  );
}

function Panel({ data }: { data: TrustForwardLab }) {
  const signals = data.signals ?? [];
  const k = signals.length;
  return (
    <div className="space-y-3">
      <p className="text-xs text-text-secondary">
        Every night the server scores every MSCI World stock on {k} published signals and freezes the result before the next
        day's prices exist; the next night it records how each signal's long-short mix did. Nothing here changes a pick.
        A signal "works" only when its evidence passes {fmtE(k / data.alpha)} for the strongest of the {k} (e-BH at{" "}
        {Math.round(data.alpha * 100)} %), so testing many signals does not make a lucky one look proven. Recorded from{" "}
        {fmtDay(data.start)}.
      </p>
      <ul className="space-y-2">
        {signals.map((s) => (
          <SignalRow key={s.signal} s={s} />
        ))}
      </ul>
      <p className="text-xs text-text-muted">{data.note}</p>
    </div>
  );
}

/** "Forward-test lab": published signals scored nightly before the outcome (plan 2026-11-09). */
export function ForwardLabPanel() {
  const query = useTrustForwardLab();
  return (
    <section aria-labelledby="trust-forward-lab" className="space-y-3 rounded-lg border border-border p-4">
      <h2 id="trust-forward-lab" className="flex items-center gap-2 text-sm font-semibold text-text-primary">
        <FlaskConical className="h-4 w-4 text-text-muted" aria-hidden="true" />
        Forward-test lab (scored every night, before the outcome)
      </h2>
      {query.isLoading ? (
        <Skeleton className="h-24 w-full rounded-md" />
      ) : query.isError || !query.data ? (
        <p className="text-sm text-text-secondary" role="status">
          The forward-test lab is unavailable right now.
        </p>
      ) : (
        <Panel data={query.data} />
      )}
    </section>
  );
}
