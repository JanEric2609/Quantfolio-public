import { ChevronRight } from "lucide-react";
import { Badge } from "../../components/ui/badge";
import { formatCurrency } from "../../lib/format";
import type { PlanAction } from "../../lib/api";

const ACTION_BADGE: Record<PlanAction["kind"], { label: string; variant: "warning" | "secondary" | "danger" }> = {
  savings_plan: { label: "Savings plan", variant: "secondary" },
  order: { label: "Single order", variant: "warning" },
  sale: { label: "Sale", variant: "danger" },
  one_off: { label: "Optional one-off", variant: "secondary" },
};

/** One required or optional step: what, where, how much; the "how to" sits behind an expander. */
export function ActionRow({ action }: { action: PlanAction }) {
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


/** This month's buys summed per broker: the one-line "where" above the steps. Sales are left out. */
export function buysByBroker(actions: PlanAction[], fallback: string): [string, number][] {
  const out = new Map<string, number>();
  for (const a of actions) {
    if (a.kind === "sale" || a.kind === "one_off") continue;
    const where = a.broker_label ?? fallback;
    out.set(where, (out.get(where) ?? 0) + a.amount_eur);
  }
  return [...out.entries()];
}

