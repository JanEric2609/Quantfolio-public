import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { CheckCircle2, Lock } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Skeleton } from "../ui/skeleton";
import { getMonthlyPlan, type PlanSleeve } from "../../lib/api";

function Consequence({ sleeve }: { sleeve: PlanSleeve }) {
  return (
    <div className="flex items-start gap-3">
      {sleeve.unlocked ? (
        <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
      ) : (
        <Lock className="mt-0.5 h-4 w-4 shrink-0 text-text-secondary" />
      )}
      <div className="min-w-0 text-sm">
        <p className="font-medium text-text-primary">
          {sleeve.label}:{" "}
          {sleeve.unlocked
            ? `may receive money, up to ${sleeve.max_pct} % of the book`
            : "receives no money (target 0 %)"}
        </p>
        <p className="text-text-secondary">{sleeve.status}</p>
      </div>
    </div>
  );
}

/**
 * Every gate in consequences (report Phase 5): what the current evidence
 * lets money do, read from the same plan the "This month" page shows.
 */
export function EvidenceConsequences() {
  const plan = useQuery({ queryKey: ["plan", "month"], queryFn: getMonthlyPlan });
  const sleeves = (plan.data?.sleeves ?? []).filter((s) => s.key !== "core");

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">What this means for your money</CardTitle>
        <CardDescription>
          The core always gets the savings plan. The other two parts of the book get money only once their
          evidence check passes. See <Link to="/plan" className="text-accent underline-offset-2 hover:underline">This month</Link>.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {plan.isLoading ? (
          <>
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </>
        ) : plan.isError ? (
          <p className="text-sm text-text-secondary">Could not load the plan, so the consequences are not shown.</p>
        ) : (
          sleeves.map((s) => <Consequence key={s.key} sleeve={s} />)
        )}
      </CardContent>
    </Card>
  );
}
