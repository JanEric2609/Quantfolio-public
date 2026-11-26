import type { ReactNode } from "react";
import { AlertTriangle, CalendarClock, CheckCircle2, Hourglass, Lock } from "lucide-react";
import { Badge } from "../../components/ui/badge";
import { Card } from "../../components/ui/card";
import { Skeleton } from "../../components/ui/skeleton";
import { ErrorState } from "../../components/composed/ErrorState";
import { Disclosure } from "../../components/composed/Disclosure";
import { EvidenceChip } from "./EvidenceChip";
import { FamilyTests } from "./FamilyTests";
import { FundsPanel } from "./FundsPanel";
import { HistoryPanel } from "./HistoryPanel";
import { ForwardLabPanel } from "./ForwardLabPanel";
import { PlanBandsPanel } from "./PlanBandsPanel";
import { RiskBandsPanel } from "./RiskBandsPanel";
import { RankingPanel } from "./RankingPanel";
import { ReliabilityPlot } from "./ReliabilityPlot";
import { TrustGlossary } from "./TrustGlossary";
import { TypeSummary } from "./TypeSummary";
import { type ChipTone, STATE_META, assumptionLabel, firstResolutionDue, fmtDay, fmtE, fmtPct, fmtPp, timeToKnowText } from "./trustFormat";
import { useTrustVerdict } from "./useTrust";
import type { TrustState, TrustVerdict } from "../../lib/api";
import { formatNumber } from "../../lib/format";

function BigNumber({ label, children, sub }: { label: string; children: ReactNode; sub?: ReactNode }) {
  return (
    <Card className="space-y-1 p-4">
      <p className="text-xs uppercase tracking-wide text-text-muted">{label}</p>
      <div className="font-mono text-2xl font-semibold tabular-nums text-text-primary">{children}</div>
      {sub ? <p className="text-xs text-text-secondary">{sub}</p> : null}
    </Card>
  );
}

const TONE_BORDER: Record<ChipTone, string> = {
  neutral: "border-l-slate-500", good: "border-l-green-500", warn: "border-l-amber-500", bad: "border-l-red-500",
};
const TONE_ICON: Record<ChipTone, string> = {
  neutral: "bg-slate-500/15 text-slate-300", good: "bg-green-500/15 text-green-400",
  warn: "bg-amber-500/15 text-amber-400", bad: "bg-red-500/15 text-red-400",
};

/** The state the headline speaks about: F1 when it crossed, else the selection test the headline reads. */
function heroState(data: TrustVerdict): TrustState {
  if (data.state === "skill" || data.state === "harm") return data.state;
  const sel = data.daily_tests?.selection_weighted ?? data.daily_tests?.selection;
  const s = sel?.state as string | undefined;
  return s === "skill" || s === "harm" ? s : data.state;
}

/** Two bars: days gathered of the days a first verdict needs, and evidence against the bar (log scale). */
function VerdictProgress({ data }: { data: TrustVerdict }) {
  const f1 = data.daily_tests?.families.find((f) => f.family === "F1");
  if (!f1 || !data.daily_tests) return null;
  const minDays = data.daily_tests.min_days_for_state;
  const dayPct = Math.min(100, (f1.n_days / minDays) * 100);
  const logPct = (e: number) => Math.min(100, Math.max(0, (Math.log(Math.max(e, 1)) / Math.log(f1.threshold)) * 100));
  return (
    <Card className="grid gap-4 p-4 sm:grid-cols-3" data-testid="trust-progress">
      <ProgressBar label="Days before a verdict can show" pct={dayPct} text={`${f1.n_days} of ${minDays}`} />
      <ProgressBar label="Evidence for skill" pct={logPct(f1.e_skill)} text={`${fmtE(f1.e_skill)} of ${f1.threshold}`} tone="bg-green-500" />
      <ProgressBar label="Evidence of harm" pct={logPct(f1.e_harm)} text={`${fmtE(f1.e_harm)} of ${f1.threshold}`} tone="bg-red-500" />
    </Card>
  );
}

function ProgressBar({ label, pct, text, tone = "bg-accent" }: { label: string; pct: number; text: string; tone?: string }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="text-text-secondary">{label}</span>
        <span className="font-mono text-text-primary">{text}</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-surface-2" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${Math.max(pct, 2)}%` }} />
      </div>
    </div>
  );
}

function VerdictSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading the verdict">
      <Skeleton className="h-20 w-full rounded-md" />
      <div className="grid gap-3 sm:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-24 w-full rounded-md" />
        ))}
      </div>
      <Skeleton className="h-40 w-full rounded-md" />
    </div>
  );
}

function ThreeNumbers({ data }: { data: TrustVerdict }) {
  const f1 = data.daily_tests?.families.find((f) => f.family === "F1");
  if (!f1) return null;
  const ci = f1.mean_ci_21d;
  const ttk = f1.time_to_know;
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <BigNumber
        label="Trading days in the test"
        sub={
          <>
            {f1.n_days ? `since ${fmtDay(f1.first_day)}` : `starts ${fmtDay(data.daily_tests?.start)}`} · a {assumptionLabel(ttk.assumption_unit, ttk.assumption)} edge would take {timeToKnowText(ttk)} to show
          </>
        }
      >
        <span data-testid="trust-units">{formatNumber(f1.n_days, { digits: 0 })}</span>
      </BigNumber>
      <BigNumber
        label="Picks vs your ETF"
        sub={
          f1.mean_21d != null ? (
            <>
              {ci ? `90 % interval ${fmtPp(ci[0], 2)} to ${fmtPp(ci[1], 2)}` : "interval needs more days"} · mean active
              return per 21 trading days
              {f1.posterior ? ` · chance the edge is positive ${fmtPct(f1.posterior.p_positive)} (not a verdict)` : ""}
            </>
          ) : (
            "no trading day with open picks recorded yet"
          )
        }
      >
        {f1.mean_21d != null ? fmtPp(f1.mean_21d, 2) : "—"}
      </BigNumber>
      <BigNumber
        label="Evidence"
        sub={
          <>
            {STATE_META[data.state].meaning} e-value {fmtE(f1.e_skill)} for skill, {fmtE(f1.e_harm)} for harm; one verdict
            needs {f1.threshold}.
          </>
        }
      >
        <EvidenceChip type={{ state: f1.state, n: f1.n_days, n_needed: null, benchmarked: true, type: "ideas" }} />
      </BigNumber>
    </div>
  );
}

function FirstResolutions({ data }: { data: TrustVerdict }) {
  const due = data.first_resolution_due ?? firstResolutionDue(data.types);
  return (
    <Card className="flex items-start gap-3 p-4" role="status">
      <CalendarClock className="mt-0.5 h-5 w-5 shrink-0 text-text-muted" aria-hidden="true" />
      <div className="space-y-1 text-sm">
        <p className="font-medium text-text-primary">
          {due ? `First resolutions due ${fmtDay(due)}.` : "Nothing has been scored yet, and nothing is waiting to be."}
        </p>
        <p className="text-text-secondary">
          Each call is judged after its own horizon against your ETF. Until then there is no score, only progress.
          {due ? "" : " Run Discover or let the advisor loop trade to start the record."}
        </p>
      </div>
    </Card>
  );
}

export function VerdictTab() {
  const query = useTrustVerdict();

  if (query.isLoading) return <VerdictSkeleton />;
  if (query.isError || !query.data) {
    return (
      <ErrorState
        title="Couldn't load the verdict"
        body={query.error instanceof Error ? query.error.message : "The track record could not be read."}
        onRetry={() => query.refetch()}
      />
    );
  }

  const data = query.data;
  const hero = heroState(data);
  const withReliability = data.types.filter((t) => t.reliability.length > 0);

  return (
    <div className="space-y-6">
      <section aria-labelledby="trust-headline" className="space-y-3">
        <Card className={`flex flex-wrap items-center justify-between gap-4 border-l-4 p-5 ${TONE_BORDER[STATE_META[hero].tone]}`}>
          <div className="flex min-w-0 items-start gap-4">
            <span className={`mt-0.5 inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full ${TONE_ICON[STATE_META[hero].tone]}`}>
              {hero === "skill" ? <CheckCircle2 className="h-5 w-5" aria-hidden="true" /> : hero === "harm" ? <AlertTriangle className="h-5 w-5" aria-hidden="true" /> : <Hourglass className="h-5 w-5" aria-hidden="true" />}
            </span>
            <div className="min-w-0">
              <p className="text-xs uppercase tracking-wide text-text-muted">The verdict</p>
              <h2 id="trust-headline" className="max-w-3xl text-lg font-semibold text-text-primary" data-testid="trust-headline">
                {data.headline}
              </h2>
            </div>
          </div>
          {data.frozen_at_issue ? (
            <Badge variant="outline" className="gap-1.5" title="Calls are stored when made and never rewritten.">
              <Lock className="h-3 w-3" aria-hidden="true" />
              Frozen at issue time
            </Badge>
          ) : null}
        </Card>
        <ThreeNumbers data={data} />
        <VerdictProgress data={data} />
      </section>

      {data.resolved_calls === 0 ? <FirstResolutions data={data} /> : null}

      <Disclosure
        title="The pre-registered tests"
        summary="One card per test: days so far, edge vs your ETF, evidence over time"
        testId="trust-tests-details"
      >
        {data.daily_tests ? <FamilyTests tests={data.daily_tests} /> : null}
        <RankingPanel />
      </Disclosure>

      <Disclosure title="Per rebalance date" summary="Picks of each date as one basket (descriptive)">
      <section aria-labelledby="trust-types" className="space-y-3">
        <div>
          <h2 id="trust-types" className="text-sm font-semibold text-text-primary">
            Per rebalance date (descriptive)
          </h2>
          <p className="text-xs text-text-secondary">
            The picks of one date as one basket, judged after their horizon. The verdict on each row comes from the test
            above; the older per-date evidence is shown as a secondary number.
          </p>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          {data.types.map((t) => (
            <TypeSummary key={t.type} type={t} verdict={data} />
          ))}
        </div>
        {data.legacy_method_note ? <p className="text-[11px] text-text-muted">{data.legacy_method_note}</p> : null}
      </section>
      </Disclosure>

      <Disclosure title="Calibration" summary="Do stated probabilities match what happened?">
      <section aria-labelledby="trust-calibration" className="space-y-3">
        <h2 id="trust-calibration" className="text-sm font-semibold text-text-primary">
          Calibration
        </h2>
        {withReliability.length > 0 ? (
          <div className="grid gap-3 lg:grid-cols-2">
            {withReliability.map((t) => (
              <ReliabilityPlot key={t.type} type={t} />
            ))}
          </div>
        ) : (
          <p className="text-sm text-text-secondary">
            The reliability plot appears once {data.min_calls_for_reliability ?? "enough"} calls with a stated probability have resolved. Below that, a
            handful of dots would look like information and be noise.
          </p>
        )}
        <p className="text-xs text-text-muted">
          No probability is stated for a pick until {data.min_n_eff_for_probability ?? 100} issue dates have resolved: picks
          made on one date share one market move, so they count once. Until then Discover shows each pick's rank among the
          stocks it scored, and how often that tier has beaten your ETF. The advisor's and the mandates' confidence is the
          model's own confidence, not a probability.
        </p>
      </section>
      </Disclosure>

      <Disclosure title="Forecast bands, forward-test lab, history and funds" summary="Secondary evidence, each checked before the outcome">
        <RiskBandsPanel />
        <PlanBandsPanel />
        <ForwardLabPanel />
        <HistoryPanel />
        <FundsPanel />
      </Disclosure>

      <Disclosure title="Glossary and method" summary="What the terms mean">
        <TrustGlossary methodNote={data.method_note} verdict={data} />
      </Disclosure>
    </div>
  );
}
