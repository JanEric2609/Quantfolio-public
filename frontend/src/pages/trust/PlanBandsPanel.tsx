import { CalendarRange } from "lucide-react";
import type { TrustPlanBandModel, TrustPlanBands } from "../../lib/api";
import { formatCurrency } from "../../lib/format";
import { Badge } from "../../components/ui/badge";
import { Skeleton } from "../../components/ui/skeleton";
import { fmtDay, fmtPct } from "./trustFormat";
import { useTrustPlanBands } from "./useTrust";

/** Years that do not overlap before the coverage interval says anything. */
const MIN_YEARS = 5;
const NOMINAL = 0.8;

const eur = (v: number) => formatCurrency(v, "EUR", { digits: 0 });

function verdict(m: TrustPlanBandModel): { label: string; variant: "secondary" | "success" | "danger" | "warning" } {
  if (m.n_independent < MIN_YEARS || m.coverage_low == null || m.coverage_high == null) {
    return { label: "Too early", variant: "secondary" };
  }
  if (m.coverage_high < NOMINAL) return { label: "Band too narrow", variant: "danger" };
  if (m.coverage_low > NOMINAL) return { label: "Band too wide", variant: "warning" };
  return { label: "No sign of trouble", variant: "success" };
}

function ModelRow({ m }: { m: TrustPlanBandModel }) {
  const v = verdict(m);
  const b = m.latest;
  return (
    <div className="space-y-1" data-testid={`plan-band-${m.model}`}>
      <h3 className="flex flex-wrap items-center gap-2 text-sm font-semibold text-text-primary">
        {m.label}
        <span className="text-[11px] font-normal text-text-muted">as of {fmtDay(b.forecast_day)}</span>
        <Badge variant={v.variant} className="w-fit text-xs">
          {v.label}
        </Badge>
      </h3>
      <p className="text-xs text-text-secondary">
        To {fmtDay(b.end_day)}, in 8 years of 10: a real return between {fmtPct(b.p10, 0)} and {fmtPct(b.p90, 0)}{" "}
        (median {fmtPct(b.p50, 0)}). Your book would then be worth {eur(b.value_p10)} to {eur(b.value_p90)} in today's
        euros, with the planned {eur(b.monthly_contribution)} a month.
      </p>
      <p className="text-xs text-text-muted">
        {m.n_scored === 0
          ? `Nothing to check yet: the first band is checked on ${fmtDay(m.first_check)}.`
          : `Checked ${m.n_scored}: ${m.inside} inside, ${m.below} below, ${m.above} above (expected 8 in 10 inside). ` +
            `${m.n_independent} of them cover years that do not overlap` +
            (m.coverage_low != null && m.coverage_high != null
              ? `; inside-rate interval ${fmtPct(m.coverage_low, 0)} to ${fmtPct(m.coverage_high, 0)}.`
              : ".")}
      </p>
    </div>
  );
}

function Panel({ data }: { data: TrustPlanBands }) {
  const models = data.models ?? [];
  if (models.length === 0) {
    return (
      <p className="text-sm text-text-secondary" role="status">
        No plan band yet: the first is stored on the first evening of the month with a synced book.
      </p>
    );
  }
  return (
    <div className="space-y-3">
      {models.map((m) => (
        <ModelRow key={m.model} m={m} />
      ))}
      {data.note && <p className="text-xs text-text-muted">{data.note}</p>}
    </div>
  );
}

/** "Plan bands": next year's range for the book, stored monthly before the fact and checked a year later (plan D6). */
export function PlanBandsPanel() {
  const query = useTrustPlanBands();
  return (
    <section aria-labelledby="trust-plan-bands" className="space-y-3 rounded-lg border border-border p-4">
      <h2 id="trust-plan-bands" className="flex items-center gap-2 text-sm font-semibold text-text-primary">
        <CalendarRange className="h-4 w-4 text-text-muted" aria-hidden="true" />
        Plan bands (next year, stored before the fact)
      </h2>
      {query.isLoading ? (
        <Skeleton className="h-24 w-full rounded-md" />
      ) : query.isError || !query.data ? (
        <p className="text-sm text-text-secondary" role="status">
          The plan bands are unavailable right now.
        </p>
      ) : (
        <Panel data={query.data} />
      )}
    </section>
  );
}
