import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { Button } from "../ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Input } from "../ui/input";
import {
  clearTrading212PiePlan,
  getScalableSavingsPlans,
  getTrading212SavingsPlans,
  setTrading212PiePlan,
} from "../../lib/api";
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
  pieId?: string;
  declared?: boolean;
}

const SCHEDULE: Record<string, string> = {
  DAILY: "daily", WEEKLY: "weekly", BIWEEKLY: "every two weeks", MONTHLY: "monthly",
};

/** Trading 212 cannot report a pie's plan, so the owner says what it is. Stored in Quantfolio only. */
function PiePlanControl({ pieId, name, declared }: { pieId: string; name: string; declared: boolean }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [amount, setAmount] = useState("");
  const [frequency, setFrequency] = useState("MONTHLY");
  const parsed = Number(amount.replace(",", "."));
  const valid = amount.trim() !== "" && Number.isFinite(parsed) && parsed > 0;
  const refresh = () => {
    setOpen(false);
    void queryClient.invalidateQueries({ queryKey: ["trading212-savings-plans"] });
    void queryClient.invalidateQueries({ queryKey: ["plan", "month"] });
  };
  const save = useMutation({ mutationFn: () => setTrading212PiePlan(pieId, parsed, frequency), onSuccess: refresh });
  const clear = useMutation({ mutationFn: () => clearTrading212PiePlan(pieId), onSuccess: refresh });
  const failed = save.isError || clear.isError;

  if (!open) {
    return (
      <div className="mt-1 flex flex-wrap items-center gap-3 text-xs">
        <button type="button" className="text-accent underline-offset-2 hover:underline" onClick={() => setOpen(true)}>
          {declared ? "Change the plan" : "Set the recurring plan"}
        </button>
        {declared && (
          <button
            type="button"
            className="text-text-secondary underline-offset-2 hover:underline"
            disabled={clear.isPending}
            onClick={() => clear.mutate()}
          >
            Not a plan
          </button>
        )}
        {failed && <span role="alert" className="text-danger">Could not save. Try again.</span>}
      </div>
    );
  }
  return (
    <form
      className="mt-2 flex flex-wrap items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) save.mutate();
      }}
    >
      <label className="text-xs text-text-secondary">
        Amount (€)
        <Input
          className="mt-1 h-9 w-24"
          inputMode="decimal"
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
          aria-label={`Amount of ${name}`}
          autoFocus
        />
      </label>
      <label className="text-xs text-text-secondary">
        How often
        <select
          className="mt-1 block h-9 rounded-md border border-border bg-transparent px-2 text-sm text-text-primary"
          value={frequency}
          onChange={(event) => setFrequency(event.target.value)}
        >
          {Object.entries(SCHEDULE).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <Button type="submit" size="sm" className="h-9" disabled={!valid || save.isPending}>Save</Button>
      <Button type="button" size="sm" variant="ghost" className="h-9" onClick={() => setOpen(false)}>Cancel</Button>
      {save.isError && <span role="alert" className="w-full text-xs text-danger">Could not save. Try again.</span>}
    </form>
  );
}

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
      key: `trading212-${plan.pie_id ?? plan.isin ?? plan.name}-${i}`,
      name: plan.name || plan.isin || "Recurring investment",
      isin: plan.isin,
      amount: plan.amount,
      schedule: [
        plan.frequency && SCHEDULE[plan.frequency]
          ? SCHEDULE[plan.frequency]
          : plan.pie_id ? "last buy, not counted"
            : plan.runs > 1 ? "schedule not recognised" : "schedule shows after the next run",
        plan.next_execution_date ? `next ${formatDate(plan.next_execution_date)}` : null,
        plan.declared ? "set by you" : null,
      ].filter(Boolean).join(", "),
      inferred: plan.inferred,
      pieId: plan.pie_id ?? undefined,
      declared: Boolean(plan.declared),
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
          As of each broker's last sync. Change them in the broker's app; Quantfolio never changes anything there.
          {rows.some((r) => r.inferred)
            ? " Trading 212 does not report its plans, so they are read back from the orders they placed."
            : ""}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y divide-border">
          {rows.map((plan) => (
            <li key={plan.key} className="py-2 text-sm">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                    <BrokerBadge source={plan.source} />
                    <span className="min-w-0 break-words">{plan.name}</span>
                  </div>
                  {plan.isin ? <div className="font-mono text-xs text-text-muted">{plan.isin}</div> : null}
                </div>
                <div className="shrink-0 text-right">
                  <div className="font-mono tabular-nums">{plan.amount != null ? formatCurrency(Number(plan.amount)) : "—"}</div>
                  <div className="text-xs text-text-muted">{plan.schedule}</div>
                </div>
              </div>
              {plan.pieId ? <PiePlanControl pieId={plan.pieId} name={plan.name} declared={Boolean(plan.declared)} /> : null}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
