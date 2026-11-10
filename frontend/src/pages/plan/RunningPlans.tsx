import { AlertTriangle } from "lucide-react";
import { Link } from "react-router-dom";
import { formatCurrency, formatDate, formatDateTime } from "../../lib/format";
import type { PlanCash, PlanSavingsPlans, RunningSavingsPlan } from "../../lib/api";

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
          {p.declared ? " · set by you" : p.inferred ? " · read from its orders" : ""}
        </span>
      </span>
      <span className="shrink-0 tabular-nums text-text-primary">
        {p.monthly_eur != null
          ? `${formatCurrency(p.monthly_eur, "EUR")}/month`
          : p.frequency === "UNKNOWN"
            ? `${formatCurrency(p.amount_eur, "EUR")}, ${p.pie_id ? "not counted" : (p.runs ?? 0) > 1 ? "schedule not recognised" : "ran once so far"}`
            : `${formatCurrency(p.amount_eur, "EUR")} ${p.frequency.toLowerCase()}`}
      </span>
    </li>
  );
}

/** Savings plans the brokers already run, grouped by sleeve, checked against the monthly budget. */
export function RunningPlans({ running, contribution }: { running: PlanSavingsPlans; contribution: number }) {
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
export function CashBreakdown({ cash }: { cash: PlanCash }) {
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

