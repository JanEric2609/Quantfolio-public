import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, CalendarCheck, Info, RefreshCw, Settings2 } from "lucide-react";
import { CrashGuardBanner } from "../components/composed/CrashGuardBanner";
import { PageHeader } from "../components/composed/PageHeader";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/skeleton";
import { formatCurrency, formatDate, formatDateTime, formatMonth } from "../lib/format";
import { getMonthlyPlan } from "../lib/api";
import { ActionRow, buysByBroker } from "./plan/ActionList";
import { CashBreakdown, RunningPlans } from "./plan/RunningPlans";
import { EtfLines, EvidenceBlock } from "./plan/EvidenceBlock";
import { SleeveRow } from "./plan/SleeveRows";
import { StatusStrip } from "./plan/StatusStrip";
import { Details, pct } from "./plan/shared";

/**
 * "This month" answers one question: what do I do with this month's money?
 * 1. the action (amount, where, steps); 2. the plan against its targets after
 * this month; 3. everything else collapsed under "More detail".
 */
export function ThisMonthPage() {
  const plan = useQuery({ queryKey: ["plan", "month"], queryFn: getMonthlyPlan });

  if (plan.isLoading) {
    return (
      <div className="space-y-6">
        <PageHeader title="This month" icon={<CalendarCheck className="h-6 w-6" />} />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-56 w-full" />
      </div>
    );
  }

  if (plan.isError || !plan.data) {
    return (
      <div className="space-y-6">
        <PageHeader title="This month" icon={<CalendarCheck className="h-6 w-6" />} />
        <Card>
          <CardContent className="flex items-center justify-between gap-3 pt-6">
            <div className="flex items-center gap-2 text-sm text-danger">
              <AlertTriangle className="h-4 w-4" />
              {plan.error instanceof Error ? plan.error.message : "Could not load this month's plan."}
            </div>
            <Button variant="outline" size="sm" onClick={() => plan.refetch()}>
              <RefreshCw className="mr-2 h-4 w-4" /> Retry
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const data = plan.data;
  const budget = data.tracking_error_budget ?? { label: "Unknown", tilt_max_pct: 0, satellite_max_pct: 0 };
  const actions = data.actions ?? [];
  const sleeves = data.sleeves ?? [];
  const notes = data.notes ?? [];
  const running = data.savings_plans && data.savings_plans.items.length > 0 ? data.savings_plans : null;
  const required = actions.filter((a) => a.kind !== "one_off");
  const optional = actions.filter((a) => a.kind === "one_off");
  const where = buysByBroker(required, data.broker_label ?? "your broker");
  const etf = data.etf_suggestions;

  return (
    <div className="space-y-6">
      <PageHeader
        title="This month"
        subtitle={`${formatMonth(data.month)} · ${formatCurrency(data.contribution_eur, "EUR")} to invest`}
        icon={<CalendarCheck className="h-6 w-6" />}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link to="/settings/profile#monthly-plan"><Settings2 className="mr-2 h-4 w-4" /> Plan settings</Link>
          </Button>
        }
      />

      <CrashGuardBanner />

      {/* 1. The answer: what to do with this month's money. */}
      <Card className={data.no_change ? "border-success/40" : undefined} data-testid="month-action">
        <CardHeader className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium uppercase tracking-wide text-text-secondary">What to do</span>
            {data.no_change && <Badge variant="success">No change</Badge>}
            {!data.never_sells && <Badge variant="warning">Rebalance</Badge>}
          </div>
          <CardTitle className="text-2xl leading-snug">{data.headline}</CardTitle>
          {where.length > 0 && (
            <p className="text-sm text-text-secondary" data-testid="month-where">
              {where.map(([broker, eur], i) => (
                <span key={broker}>
                  {i > 0 ? " · " : ""}
                  <span className="font-medium tabular-nums text-text-primary">{formatCurrency(eur, "EUR")}</span> at {broker}
                </span>
              ))}
            </p>
          )}
          {!data.has_holdings && (
            <CardDescription>
              No holdings yet, so the split below starts from an empty book. Sync DKB, Scalable Capital or Trading 212, and enter
              positions held elsewhere under Portfolio → Holdings, to see your sleeves.
            </CardDescription>
          )}
        </CardHeader>
        {required.length > 0 && (
          <CardContent>
            {required.map((a, i) => <ActionRow key={`${i}-${a.sleeve}-${a.kind}-${a.isin ?? ""}`} action={a} />)}
          </CardContent>
        )}
        {optional.length > 0 && (
          <CardContent className="pt-0">
            <div className="text-xs font-medium uppercase tracking-wide text-text-secondary">Optional, from cash above your reserve</div>
            {optional.map((a, i) => <ActionRow key={`opt-${i}-${a.sleeve}-${a.isin ?? ""}`} action={a} />)}
          </CardContent>
        )}
        {notes.length > 0 && (
          <CardContent className="space-y-2 pt-0">
            {notes.map((n) => (
              <p key={n} className="flex gap-2 text-sm text-text-secondary">
                <Info className="mt-0.5 h-4 w-4 shrink-0" /> <span>{n}</span>
              </p>
            ))}
          </CardContent>
        )}
      </Card>

      <StatusStrip plan={data} />

      {/* Everything else, collapsed. */}
      <section className="space-y-3" aria-labelledby="month-more">
        <h2 id="month-more" className="text-sm font-semibold text-text-primary">More detail</h2>
        {sleeves.length > 0 && (
          <Details title="Your plan vs target" summary={`book ${formatCurrency(data.book_eur, "EUR")}`}>
            <div data-testid="month-plan-vs-target">
              <p className="pb-2 text-sm text-text-secondary">
                The bar is today&apos;s share, the tick the target, the faint band the cap. Locked parts get money only
                once their evidence check passes.
              </p>
              <ul className="divide-y divide-border">
                  {sleeves.map((s) => (
                    <SleeveRow
                      key={s.key}
                      sleeve={s}
                      lookThrough={s.key === "core" ? data.core_look_through : null}
                      fund={data.suggested_funds?.find((f) => f.sleeve === s.key) ?? null}
                    />
                  ))}
                </ul>
            </div>
          </Details>
        )}
        {data.cash && (
          <Details title="Cash" summary={`${formatCurrency(data.cash.investable_eur, "EUR")} investable`}>
            <CashBreakdown cash={data.cash} />
          </Details>
        )}
        {running && (
          <Details title="Savings plans that already run" summary={`${formatCurrency(running.monthly_eur, "EUR")} a month`}>
            <RunningPlans running={running} contribution={data.contribution_eur} />
          </Details>
        )}
        {etf && etf.lines.length > 0 && (
          <Details title="Bonds, cash and gold" summary={`${etf.lines.filter((l) => l.status === "under").length} below target`}>
            <EtfLines suggestions={etf} />
          </Details>
        )}
        {data.evidence && (
          <Details
            title="Evidence"
            summary={
              data.evidence.satellite.gate?.as_of
                ? `stock picks checked ${formatDate(data.evidence.satellite.gate.as_of)}`
                : undefined
            }
          >
            <EvidenceBlock evidence={data.evidence} />
          </Details>
        )}
        <Details title="How this plan is decided" summary={`${budget.label} tracking-error budget`}>
          <div className="space-y-2 text-sm text-text-secondary">
            <p>
              New money goes to whichever part of the book is below its target. In Germany every sale is a taxable event,
              and topping up with new money usually gets you back on target without one. So a sale is proposed only when a
              part that passed its evidence check sits more than {data.drift_band_pp ?? 5} points above its target and a
              year of contributions would not bring it back. Positions in a part that has not passed are never sold.
            </p>
            <p>
              A factor tilt (up to {pct(budget.tilt_max_pct)}) and stock picks (up to {pct(budget.satellite_max_pct)})
              only receive money once they pass their evidence check; until then their target is 0 % and all new money goes
              to the core. Nothing else, the LLM paper loop included, sets a target for your real book. Stock buys under{" "}
              {formatCurrency(data.min_order_eur, "EUR")} go to the core instead, so the order fee stays at or below 1 %.
              Each action names its broker and that broker&apos;s fee: new money goes to{" "}
              {data.broker_choice === "auto" ? "the cheapest synced depot" : data.broker_label ?? "DKB"}, and a sale is
              made where the position sits, starting with the smallest gain per euro, because FIFO runs per depot. Cash
              above your emergency reserve is offered as an optional one-off.
            </p>
            {data.mix_note && (
              <p>
                <span className="font-medium text-text-primary">Why 100 % equity? </span>
                {data.mix_note}
              </p>
            )}
            {data.acc_dist_note && (
              <p>
                <span className="font-medium text-text-primary">Accumulating or distributing? </span>
                {data.acc_dist_note}
              </p>
            )}
            {etf && etf.info.length > 0 && (
              <div className="space-y-1">
                <p>
                  <span className="font-medium text-text-primary">Other indices. </span>
                  These indices are not part of your plan, so they are never suggested. Shown for information only.
                </p>
                <ul className="list-disc pl-5">
                  {etf.info.map((i) => (
                    <li key={`${i.index}-${i.isin}`}>{i.index}{i.name ? ` — ${i.name}` : ""}</li>
                  ))}
                </ul>
                {etf.covered.length > 0 && (
                  <p className="text-xs text-text-muted">
                    Already covered: {etf.covered.map((c) => `${c.index} by ${c.held_by}`).join("; ")}.
                  </p>
                )}
              </div>
            )}
            <p>
              Status of the checks: <Link to="/evidence" className="text-accent underline-offset-2 hover:underline">Evidence</Link>.
              {data.holdings_synced_at ? ` Synced holdings as of ${formatDateTime(data.holdings_synced_at)}.` : ""} Not
              investment advice.
            </p>
          </div>
        </Details>
      </section>
    </div>
  );
}
