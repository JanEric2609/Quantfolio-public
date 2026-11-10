import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { formatCurrency } from "../../lib/format";
import { getCrashGuard, type MonthlyPlan } from "../../lib/api";

function Chip({ label, value, to, hash }: { label: string; value: string; to?: string; hash?: string }) {
  const body = (
    <>
      <span className="text-text-muted">{label}</span> <span className="font-medium text-text-primary">{value}</span>
    </>
  );
  const cls = "inline-flex items-center gap-1.5 rounded-full border border-border bg-surface-1 px-3 py-1 text-xs";
  if (hash) return <a href={hash} className={`${cls} hover:border-accent`}>{body}</a>;
  if (to) return <Link to={to} className={`${cls} hover:border-accent`}>{body}</Link>;
  return <span className={cls}>{body}</span>;
}

/** One line of state: the crash guard level, the capital-ladder stage and the investable cash, each linking to its page. */
export function StatusStrip({ plan }: { plan: MonthlyPlan }) {
  const guard = useQuery({ queryKey: ["plan", "crash-guard"], queryFn: getCrashGuard, staleTime: 15 * 60_000 });
  const level = guard.data?.market?.level_label;
  const ladder = plan.evidence?.satellite.ladder;
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="status-strip" aria-label="Where things stand">
      <Chip label="Crash guard" value={level ?? "–"} to="/" />
      <Chip
        label="Capital ladder"
        value={ladder ? `stage ${ladder.stage}, ${ladder.stage_name}` : "Paper stage"}
        to="/graduation"
      />
      {plan.cash?.savings_pot_eur ? (
        <Chip label="Savings pot" value={formatCurrency(plan.cash.savings_pot_eur, "EUR")} hash="#month-more" />
      ) : null}
      {plan.cash && (
        <Chip label="Investable cash" value={formatCurrency(plan.cash.investable_eur, "EUR")} hash="#month-more" />
      )}
    </div>
  );
}
