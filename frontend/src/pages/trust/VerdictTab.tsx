import type { ReactNode } from "react";
import { CalendarClock, Lock } from "lucide-react";
import { Badge } from "../../components/ui/badge";
import { Card } from "../../components/ui/card";
import { Skeleton } from "../../components/ui/skeleton";
import { ErrorState } from "../../components/composed/ErrorState";
import { EvidenceChip } from "./EvidenceChip";
import { ReliabilityPlot } from "./ReliabilityPlot";
import { TrustGlossary } from "./TrustGlossary";
import { TypeSummary } from "./TypeSummary";
import { STATE_META, aboutHundreds, firstResolutionDue, fmtDay, fmtInterval, fmtPp } from "./trustFormat";
import { useTrustVerdict } from "./useTrust";
import type { TrustVerdict } from "../../lib/api";

function BigNumber({ label, children, sub }: { label: string; children: ReactNode; sub?: ReactNode }) {
  return (
    <Card className="space-y-1 p-4">
      <p className="text-xs uppercase tracking-wide text-text-muted">{label}</p>
      <div className="font-mono text-2xl font-semibold tabular-nums text-text-primary">{children}</div>
      {sub ? <p className="text-xs text-text-secondary">{sub}</p> : null}
    </Card>
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
  const needed = data.n_needed ?? data.types.find((t) => t.n_needed != null)?.n_needed ?? null;
  const units = data.resolved_units ?? 0;
  const target = Math.round((data.target_hit_rate ?? 0.55) * 100);
  const skill = data.skill;
  const skillCi = skill && skill.ci_low != null && skill.ci_high != null ? fmtInterval([skill.ci_low, skill.ci_high], "pp") : null;
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      <BigNumber
        label="Independent rebalance dates"
        sub={
          <>
            from {data.resolved_calls} resolved calls
            {needed ? ` · about ${needed} dates needed to tell a ${target} % hit rate from a coin flip` : ""}
          </>
        }
      >
        <span data-testid="trust-units">
          {units}
          {needed ? <span className="text-base font-normal text-text-muted"> of ~{aboutHundreds(needed)}</span> : null}
        </span>
      </BigNumber>
      <BigNumber
        label="Skill vs your ETF"
        sub={
          skill ? (
            <>
              {skillCi ? `90 % interval ${skillCi} (Newey-West)` : "interval needs 8 dates"} · {skill.n} dates
              <br />
              mean excess return per rebalance date vs {skill.benchmark_label}
            </>
          ) : (
            "no resolved calls to compare yet"
          )
        }
      >
        {skill ? fmtPp(skill.value) : "—"}
      </BigNumber>
      <BigNumber
        label="Evidence"
        sub={
          <>
            {STATE_META[data.state].meaning}
            {data.n_tests ? ` Corrected for ${data.n_tests} looks (e-BH, 5 % false discoveries).` : ""}
          </>
        }
      >
        <EvidenceChip
          type={{ state: data.state, n: units, n_needed: needed, benchmarked: true, type: "ideas" }}
        />
      </BigNumber>
    </div>
  );
}

function FirstResolutions({ data }: { data: TrustVerdict }) {
  const due = firstResolutionDue(data.types);
  return (
    <Card className="flex items-start gap-3 p-4" role="status">
      <CalendarClock className="mt-0.5 h-5 w-5 shrink-0 text-text-muted" aria-hidden="true" />
      <div className="space-y-1 text-sm">
        <p className="font-medium text-text-primary">
          {due ? `First resolutions due ${fmtDay(due)}.` : "Nothing has been scored yet, and nothing is waiting to be."}
        </p>
        <p className="text-text-secondary">
          Every call is judged after its own horizon (stored with the call) against your passive core ETF; mandate
          reviews after the weeks they name. Until then this page shows how far along the record is, never a made-up score.
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
  const withReliability = data.types.filter((t) => t.reliability.length > 0);

  return (
    <div className="space-y-6">
      <section aria-labelledby="trust-headline" className="space-y-3">
        <Card className="flex flex-wrap items-start justify-between gap-3 p-5">
          <h2 id="trust-headline" className="max-w-3xl text-lg font-semibold text-text-primary" data-testid="trust-headline">
            {data.headline}
          </h2>
          {data.frozen_at_issue ? (
            <Badge variant="outline" className="gap-1.5" title="Calls are stored when made and never rewritten.">
              <Lock className="h-3 w-3" aria-hidden="true" />
              Frozen at issue time
            </Badge>
          ) : null}
        </Card>
        <ThreeNumbers data={data} />
      </section>

      {data.resolved_calls === 0 ? <FirstResolutions data={data} /> : null}

      <section aria-labelledby="trust-types" className="space-y-3">
        <h2 id="trust-types" className="text-sm font-semibold text-text-primary">
          By prediction type
        </h2>
        <div className="grid gap-3 lg:grid-cols-2">
          {data.types.map((t) => (
            <TypeSummary key={t.type} type={t} />
          ))}
        </div>
      </section>

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
            The reliability plot appears once 30 calls with a stated probability have resolved. Below that, a
            handful of dots would look like information and be noise.
          </p>
        )}
      </section>

      <TrustGlossary methodNote={data.method_note} needed={data.n_needed ?? null} target={data.target_hit_rate ?? null} />
    </div>
  );
}
