import { useQuery } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { getScalableSavingsPlans, getTrading212SavingsPlans } from "../../lib/api";
import { formatCurrency, formatDate } from "../../lib/format";
import { BrokerBadge } from "./BrokerFilter";
import { useScalableStatus } from "./ScalableSync";
import { useTrading212Status } from "./Trading212Sync";

interface PlanRow {
  source: "scalable" | "trading212";
  key: string;
  name: string;
  isin: string | null;
  amount: string | null;
  schedule: string;
  inferred: boolean;
}

const SCHEDULE: Record<string, string> = {
  DAILY: "daily", WEEKLY: "weekly", BIWEEKLY: "every two weeks", MONTHLY: "monthly",
};

/** Every broker's savings plans in one list. Read-only: they are changed in each broker's app. */
export function BrokerSavingsPlans() {
  const scalableStatus = useScalableStatus();
  const t212Status = useTrading212Status();
  const scalable = useQuery({
    queryKey: ["scalable-savings-plans"],
    queryFn: getScalableSavingsPlans,
    enabled: Boolean(scalableStatus.data?.enabled && scalableStatus.data.last_success_at),
  });
  const trading212 = useQuery({
    queryKey: ["trading212-savings-plans"],
    queryFn: getTrading212SavingsPlans,
    enabled: Boolean(t212Status.data?.enabled && t212Status.data.last_success_at),
  });

  const rows: PlanRow[] = [
    ...(scalable.data ?? []).map((plan, i) => ({
      source: "scalable" as const,
      key: `scalable-${plan.isin ?? plan.name}-${i}`,
      name: plan.name || plan.isin || "Savings plan",
      isin: plan.isin,
      amount: plan.amount,
      schedule: [plan.frequency?.toLowerCase(), plan.day_of_month ? `day ${plan.day_of_month}` : null].filter(Boolean).join(", "),
      inferred: false,
    })),
    ...(trading212.data ?? []).map((plan, i) => ({
      source: "trading212" as const,
      key: `trading212-${plan.isin ?? plan.name}-${i}`,
      name: plan.name || plan.isin || "Recurring investment",
      isin: plan.isin,
      amount: plan.amount,
      schedule: [
        plan.frequency && SCHEDULE[plan.frequency] ? SCHEDULE[plan.frequency] : "schedule shows after the next run",
        plan.next_execution_date ? `next ${formatDate(plan.next_execution_date)}` : null,
      ].filter(Boolean).join(", "),
      inferred: plan.inferred,
    })),
  ];
  if (!rows.length) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Savings plans
          <ShieldCheck className="h-4 w-4 text-text-muted" aria-label="Read-only" />
        </CardTitle>
        <CardDescription>
          As of each broker's last sync. Change them in the broker's app; Quantfolio only reads them.
          {rows.some((r) => r.inferred)
            ? " Trading 212 does not report its plans, so they are read back from the orders they placed."
            : ""}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y divide-border">
          {rows.map((plan) => (
            <li key={plan.key} className="flex items-center justify-between gap-3 py-2 text-sm">
              <div className="min-w-0">
                <div className="flex min-w-0 items-center gap-2">
                  <BrokerBadge source={plan.source} />
                  <span className="truncate">{plan.name}</span>
                </div>
                {plan.isin ? <div className="font-mono text-xs text-text-muted">{plan.isin}</div> : null}
              </div>
              <div className="shrink-0 text-right">
                <div className="font-mono tabular-nums">{plan.amount != null ? formatCurrency(Number(plan.amount)) : "—"}</div>
                <div className="text-xs text-text-muted">{plan.schedule}</div>
              </div>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
