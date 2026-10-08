import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, CalendarCheck, CheckCircle2, ChevronRight, Info, Lock, RefreshCw, Settings2 } from "lucide-react";
import { PageHeader } from "../components/composed/PageHeader";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/skeleton";
import { formatCurrency, formatDate, formatDateTime, formatMonth, formatPercentPoints } from "../lib/format";
import {
  getMonthlyPlan,
  type MonthlyPlan,
  type PlanAction,
  type PlanCash,
  type PlanSavingsPlans,
  type PlanSleeve,
  type RunningSavingsPlan,
  type SuggestedFund,
} from "../lib/api";

// The plan API reports shares in percent units (12.5 = 12,5 %), not fractions.
function pct(value: number): string {
  return formatPercentPoints(value, { digits: value >= 10 || value === 0 ? 0 : 1 });
}

/** Current share vs target; the tick marks the target, the faint band the cap. */
function SleeveBar({ sleeve }: { sleeve: PlanSleeve }) {
  const current = Math.min(100, sleeve.current_pct);
  return (
    <div className="relative h-2 w-full overflow-hidden rounded-full bg-surface-2" aria-hidden>
      {sleeve.key !== "core" && (
        <div className="absolute inset-y-0 left-0 bg-text-secondary/10" style={{ width: `${sleeve.max_pct}%` }} />
      )}
      <div
        className={`absolute inset-y-0 left-0 rounded-full ${sleeve.unlocked ? "bg-accent" : "bg-text-secondary/40"}`}
        style={{ width: `${current}%` }}
      />
      <div className="absolute inset-y-0 w-0.5 bg-text-primary" style={{ left: `calc(${Math.min(99.5, sleeve.target_pct)}%)` }} />
    </div>
  );
}

const ACTION_BADGE: Record<PlanAction["kind"], { label: string; variant: "warning" | "secondary" | "danger" }> = {
  savings_plan: { label: "Savings plan", variant: "secondary" },
  order: { label: "Single order", variant: "warning" },
  sale: { label: "Sale", variant: "danger" },
  one_off: { label: "Optional one-off", variant: "secondary" },
};

/** One required or optional step: what, where, how much; the "how to" sits behind an expander. */
function ActionRow({ action }: { action: PlanAction }) {
  const badge = ACTION_BADGE[action.kind] ?? ACTION_BADGE.order;
  return (
    <div className="border-b border-border py-3 last:border-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium text-text-primary">{action.instrument}</span>
            {action.ticker && <span className="font-mono text-xs text-text-secondary">{action.ticker}</span>}
            <Badge variant={badge.variant}>{badge.label}</Badge>
            {action.broker_label && <Badge variant="outline">{action.broker_label}</Badge>}
          </div>
        </div>
        <div className="shrink-0 font-display text-xl font-semibold tabular-nums text-text-primary">
          {action.kind === "sale" ? "−" : ""}
          {formatCurrency(action.amount_eur, "EUR")}
        </div>
      </div>
      <details className="group mt-1">
        <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-xs text-accent hover:underline">
          <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" /> How to
        </summary>
        <p className="mt-1 text-sm text-text-secondary">{action.note}</p>
      </details>
    </div>
  );
}

/** A collapsed section: the information stays, the page stays short. */
function Details({ title, summary, children }: { title: string; summary?: string; children: ReactNode }) {
  return (
    <details className="group rounded-lg border border-border bg-surface-1">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-medium text-text-primary">
        <ChevronRight className="h-4 w-4 shrink-0 transition-transform group-open:rotate-90" />
        {title}
        {summary && <span className="ml-auto min-w-0 truncate text-xs font-normal text-text-secondary">{summary}</span>}
      </summary>
      <div className="space-y-3 border-t border-border p-4">{children}</div>
    </details>
  );
}

type LookThrough = NonNullable<MonthlyPlan["core_look_through"]>;

function SleeveCard({ sleeve, lookThrough, fund }: { sleeve: PlanSleeve; lookThrough?: LookThrough | null; fund?: SuggestedFund | null }) {
  const positions = sleeve.positions ?? [];
  return (
    <Card className="min-w-0">
      <CardHeader className="space-y-1 pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex min-w-0 items-center gap-2 text-base">
            {sleeve.unlocked ? (
              <CheckCircle2 className="h-4 w-4 text-success" />
            ) : (
              <Lock className="h-4 w-4 text-text-secondary" />
            )}
            {sleeve.label}
          </CardTitle>
          <span className="shrink-0 text-xs text-text-secondary">
            {sleeve.key === "core" ? "the rest" : `cap ${pct(sleeve.max_pct)}`}
          </span>
        </div>
        <div className="font-display text-2xl font-semibold tabular-nums text-text-primary">
          {formatCurrency(sleeve.current_eur, "EUR")}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <SleeveBar sleeve={sleeve} />
        <div className="flex justify-between text-xs text-text-secondary tabular-nums">
          <span>now {pct(sleeve.current_pct)}</span>
          <span>target {pct(sleeve.target_pct)}</span>
        </div>
        <p className="text-sm text-text-secondary">{sleeve.status}</p>
        {fund && (
          <p className="text-sm text-text-secondary">
            <span className="font-medium text-text-primary">Suggested fund: {fund.isin ?? fund.name}. </span>
            {fund.why}
          </p>
        )}
        {lookThrough && (
          <p className="text-sm text-text-secondary">
            {lookThrough.em_pct != null ? (
              <>
                Looking through the funds, {pct(lookThrough.em_pct)} of the core is emerging markets; the world market
                (MSCI ACWI, FTSE All-World) holds about {pct(lookThrough.world_em_pct)}.
                {lookThrough.unknown_eur > 0 &&
                  ` The split of ${formatCurrency(lookThrough.unknown_eur, "EUR")} of it is not known and is left out.`}
              </>
            ) : (
              <>
                The emerging-markets split of your core funds is not known, so there is no look-through; the world
                market (MSCI ACWI, FTSE All-World) holds about {pct(lookThrough.world_em_pct)}.
              </>
            )}
          </p>
        )}
        {sleeve.contribution_eur > 0 && (
          <p className="text-sm text-text-primary">
            +{formatCurrency(sleeve.contribution_eur, "EUR")} this month → {pct(sleeve.after_pct)}
          </p>
        )}
        {(sleeve.sale_eur ?? 0) > 0 && (
          <p className="text-sm text-danger">
            −{formatCurrency(sleeve.sale_eur ?? 0, "EUR")} sold → {pct(sleeve.after_pct)}
          </p>
        )}
        {(sleeve.reinvest_eur ?? 0) > 0 && (
          <p className="text-sm text-text-primary">
            +{formatCurrency(sleeve.reinvest_eur ?? 0, "EUR")} from the sale → {pct(sleeve.after_pct)}
          </p>
        )}
        {positions.length > 0 && (
          <ul className="space-y-1 border-t border-border pt-2 text-sm">
            {positions.slice(0, 6).map((p) => (
              <li key={p.isin + p.name} className="flex justify-between gap-3">
                <span className="min-w-0 truncate text-text-secondary" title={p.isin}>{p.name}</span>
                <span className="shrink-0 tabular-nums text-text-primary">{formatCurrency(p.value_eur, "EUR")}</span>
              </li>
            ))}
            {positions.length > 6 && (
              <li className="text-xs text-text-secondary">+{positions.length - 6} more</li>
            )}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** A sleeve that is still locked: one line, with the way to settings. */
function LockedSleeveLine({ sleeve, fund }: { sleeve: PlanSleeve; fund?: SuggestedFund | null }) {
  return (
    <li className="flex flex-col gap-1 py-2 text-sm sm:flex-row sm:items-start sm:justify-between">
      <span className="flex min-w-0 gap-2">
        <Lock className="mt-0.5 h-4 w-4 shrink-0 text-text-secondary" />
        <span className="min-w-0">
          <span className="font-medium text-text-primary">{sleeve.label}</span>
          <span className="text-text-secondary">
            {" "}· target 0 % · you hold {formatCurrency(sleeve.current_eur, "EUR")} ({pct(sleeve.current_pct)}), cap {pct(sleeve.max_pct)}
          </span>
          <span className="block text-text-secondary">{sleeve.status}</span>
          {fund && fund.isin && (
            <span className="block text-text-secondary">Suggested fund once unlocked: {fund.isin} — {fund.name}.</span>
          )}
        </span>
      </span>
      <Link to="/settings/profile#monthly-plan" className="shrink-0 text-xs text-accent underline-offset-2 hover:underline">
        Open in settings
      </Link>
    </li>
  );
}

const SLEEVE_NAMES: Record<string, string> = { core: "Core", tilt: "Factor tilt", satellite: "Stock picks" };

function PlanLine({ p }: { p: RunningSavingsPlan }) {
  return (
    <li className="flex justify-between gap-3">
      <span className="min-w-0">
        <span className="block truncate text-text-primary" title={p.isin ?? undefined}>{p.name}</span>
        <span className="text-xs text-text-secondary">
          {p.broker_label}
          {p.next_execution_date ? ` · next ${formatDate(p.next_execution_date)}` : ""}
          {p.dynamization_rate ? ` · grows ${p.dynamization_rate} %/yr` : ""}
          {p.not_running_reason === "paused" ? " · paused" : ""}
          {p.not_running_reason === "overdue" ? " · overdue, no run seen" : ""}
        </span>
      </span>
      <span className="shrink-0 tabular-nums text-text-primary">
        {p.monthly_eur == null ? `${formatCurrency(p.amount_eur, "EUR")} ${p.frequency.toLowerCase()}` : `${formatCurrency(p.monthly_eur, "EUR")}/month`}
      </span>
    </li>
  );
}

/** Savings plans the brokers already run, grouped by sleeve, checked against the monthly budget. */
function RunningPlans({ running, contribution }: { running: PlanSavingsPlans; contribution: number }) {
  const active = running.items.filter((p) => p.running !== false);
  const stopped = running.items.filter((p) => p.running === false);
  const groups = ["core", "tilt", "satellite"]
    .map((key) => ({ key, items: active.filter((p) => p.sleeve === key) }))
    .filter((g) => g.items.length > 0);
  const picks = running.other_budget_eur ?? 0;
  return (
    <div className="space-y-3">
      <p className="text-sm text-text-secondary">
        {formatCurrency(running.monthly_eur, "EUR")} a month, read from your broker
        {running.synced_at ? ` as of ${formatDateTime(running.synced_at)}` : ""}.
      </p>
      {running.last_fetch_failed && (
        <p className="flex gap-2 text-sm text-warning">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>The last sync could not read your savings plans, so this list may be out of date. A plan you deleted can still show here.</span>
        </p>
      )}
      {groups.map((g) => (
        <div key={g.key}>
          <div className="flex justify-between text-xs font-medium uppercase tracking-wide text-text-secondary">
            <span>{SLEEVE_NAMES[g.key] ?? g.key}</span>
            <span className="tabular-nums">{formatCurrency(running.by_sleeve[g.key] ?? 0, "EUR")}/month</span>
          </div>
          <ul className="mt-1 space-y-2 text-sm">
            {g.items.map((p, i) => <PlanLine key={`${p.broker}-${p.isin ?? p.name}-${i}`} p={p} />)}
          </ul>
        </div>
      ))}
      {running.core_needed_eur != null && contribution > 0 && (
        <p className="border-t border-border pt-2 text-sm text-text-secondary">
          Budget check: of your {formatCurrency(contribution, "EUR")} a month,{" "}
          {picks > 0 ? `${formatCurrency(picks, "EUR")} goes into your own plans and ` : ""}
          the core plans need {formatCurrency(running.core_needed_eur, "EUR")}; they run{" "}
          {formatCurrency(running.by_sleeve.core ?? 0, "EUR")}.
        </p>
      )}
      {stopped.length > 0 && (
        <div>
          <div className="text-xs font-medium uppercase tracking-wide text-text-secondary">Not running (not counted)</div>
          <ul className="mt-1 space-y-2 text-sm">
            {stopped.map((p, i) => <PlanLine key={`${p.broker}-${p.isin ?? p.name}-${i}`} p={p} />)}
          </ul>
        </div>
      )}
    </div>
  );
}

/** Overnight/Tagesgeld-type savings and free broker cash against the fixed emergency reserve. Giro money never counts. */
function CashBreakdown({ cash }: { cash: PlanCash }) {
  const total = cash.savings_eur + cash.broker_cash_eur;
  return (
    <div className="space-y-1 text-sm tabular-nums">
      <p className="pb-1 text-text-secondary">
        Savings accounts and free broker cash; your giro account is spending money and never counts.
      </p>
      <div className="flex justify-between gap-3">
        <span className="text-text-secondary">Overnight / savings accounts (Scalable overnight, DKB Tagesgeld)</span>
        <span className="shrink-0">{formatCurrency(cash.savings_eur, "EUR")}</span>
      </div>
      <div className="flex justify-between"><span className="text-text-secondary">Free at the broker</span><span>{formatCurrency(cash.broker_cash_eur, "EUR")}</span></div>
      {cash.reserve_set ? (
        <>
          <div className="flex justify-between"><span className="text-text-secondary">Emergency reserve</span><span>−{formatCurrency(cash.emergency_reserve_eur, "EUR")}</span></div>
          <div className="flex justify-between border-t border-border pt-1 font-medium text-text-primary">
            <span>Investable</span><span>{formatCurrency(cash.investable_eur, "EUR")}</span>
          </div>
        </>
      ) : (
        total > 0 && (
          <p className="pt-1 text-text-secondary">
            Set your emergency reserve in the <Link to="/settings/profile#monthly-plan" className="text-accent underline-offset-2 hover:underline">plan settings</Link> to see how much is investable.
          </p>
        )
      )}
    </div>
  );
}

/** When the stock-picking evidence gate last ran, and what the factor-tilt check says. */
function EvidenceBlock({ evidence }: { evidence: NonNullable<MonthlyPlan["evidence"]> }) {
  const gate = evidence.satellite.gate;
  return (
    <div className="space-y-2 text-sm text-text-secondary">
      <p className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-text-primary">Stock picks:</span>
        {gate ? (
          <>
            checked {gate.as_of ? formatDate(gate.as_of) : "on an unknown date"} against {gate.n_trials} trials
            {gate.stale && <Badge variant="warning">out of date</Badge>}
          </>
        ) : (
          "not checked yet"
        )}
      </p>
      <p>
        <span className="font-medium text-text-primary">Factor tilt:</span>{" "}
        {evidence.tilt.unlocked
          ? "evidence passed and a tilt fund is chosen."
          : evidence.tilt.passed
            ? "evidence passed, but no tilt fund is chosen. Set one in the plan settings to use it."
            : "no factor strategy has passed its evidence check yet."}
      </p>
      <Link to="/evidence" className="text-accent underline-offset-2 hover:underline">Open the evidence page</Link>
    </div>
  );
}

/**
 * "This month": where the monthly contribution goes, and whether anything else
 * changes. Contribution-first; a sale only past the drift band.
 */
export function ThisMonthPage() {
  const plan = useQuery({ queryKey: ["plan", "month"], queryFn: getMonthlyPlan });

  if (plan.isLoading) {
    return (
      <div className="space-y-6">
        <PageHeader title="This month" icon={<CalendarCheck className="h-6 w-6" />} />
        <Skeleton className="h-40 w-full" />
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <Skeleton className="h-56 w-full" />
          <Skeleton className="h-56 w-full" />
          <Skeleton className="h-56 w-full" />
        </div>
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
  const open = sleeves.filter((s) => s.unlocked);
  const locked = sleeves.filter((s) => !s.unlocked);

  return (
    <div className="space-y-6">
      <PageHeader
        title="This month"
        subtitle={`${formatMonth(data.month)} · ${formatCurrency(data.contribution_eur, "EUR")} contribution · ${budget.label} tracking-error budget`}
        icon={<CalendarCheck className="h-6 w-6" />}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link to="/settings/profile#monthly-plan"><Settings2 className="mr-2 h-4 w-4" /> Plan settings</Link>
          </Button>
        }
      />

      <Card className={data.no_change ? "border-success/40" : undefined}>
        <CardHeader>
          <div className="flex flex-wrap items-center gap-2">
            {data.no_change && <Badge variant="success">No change</Badge>}
            {data.never_sells ? (
              <Badge variant="secondary">No sale</Badge>
            ) : (
              <Badge variant="warning">Rebalance</Badge>
            )}
          </div>
          <CardTitle className="pt-2 text-2xl leading-snug">{data.headline}</CardTitle>
          {!data.has_holdings && (
            <CardDescription>
              No holdings yet, so the split below starts from an empty book. Sync DKB or Scalable Capital, and enter
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

      <div className="space-y-3">
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
        <Details title="Your three sleeves" summary={`${open.length} active, ${locked.length} locked`}>
          {open.length > 0 && (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              {open.map((s) => (
                <SleeveCard key={s.key} sleeve={s} lookThrough={s.key === "core" ? data.core_look_through : null} fund={data.suggested_funds?.find((f) => f.sleeve === s.key) ?? null} />
              ))}
            </div>
          )}
          {locked.length > 0 && (
            <ul className="divide-y divide-border">
              {locked.map((s) => <LockedSleeveLine key={s.key} sleeve={s} fund={data.suggested_funds?.find((f) => f.sleeve === s.key) ?? null} />)}
            </ul>
          )}
        </Details>
        {data.acc_dist_note && (
          <Details title="Accumulating or distributing?" summary="why accumulating is the default">
            <p className="text-sm text-text-secondary">{data.acc_dist_note}</p>
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
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">How this is decided</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm text-text-secondary">
          <p>
            New money goes to whichever part of the book is below its target. In Germany every sale is a taxable event,
            and topping up with new money usually gets you back on target without one. So a sale is proposed only when a
            part that passed its evidence check sits more than {data.drift_band_pp ?? 5} points above its target and a
            year of contributions would not bring it back. Positions in a part that has not passed are never sold.
          </p>
          <p>
            A factor tilt (up to {pct(budget.tilt_max_pct)}) and stock picks (up to {pct(budget.satellite_max_pct)})
            only receive money once they pass their evidence check; until then their target is 0 % and all new money goes
            to the core. Nothing else, the LLM paper loop included, sets a target for your real book. Stock buys under {formatCurrency(data.min_order_eur, "EUR")} go to the core
            instead, so the order fee stays at or below 1 %. Each action names its broker and that broker's fee: new money
            goes to {data.broker_choice === "auto" ? "the cheapest synced depot" : data.broker_label ?? "DKB"}, and a sale is
            made where the position sits, starting with the smallest gain per euro, because FIFO runs per depot. Cash above
            your emergency reserve is offered as an optional one-off.
          </p>
          <p>
            Status of the checks: <Link to="/evidence" className="text-accent underline-offset-2 hover:underline">Evidence</Link>.
            Book value {formatCurrency(data.book_eur, "EUR")}
            {data.holdings_synced_at ? `, synced holdings as of ${formatDateTime(data.holdings_synced_at)}` : ""}. Not
            investment advice.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
