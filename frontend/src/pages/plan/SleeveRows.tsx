import { CheckCircle2, ChevronRight, Lock } from "lucide-react";
import { Link } from "react-router-dom";
import { formatCurrency } from "../../lib/format";
import type { MonthlyPlan, PlanSleeve, SuggestedFund } from "../../lib/api";
import { pct } from "./shared";

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

type LookThrough = NonNullable<MonthlyPlan["core_look_through"]>;

/** One sleeve in the plan-vs-target view: now → after this month against its target; the rest behind "Details". */
export function SleeveRow({ sleeve, lookThrough, fund }: { sleeve: PlanSleeve; lookThrough?: LookThrough | null; fund?: SuggestedFund | null }) {
  const positions = sleeve.positions ?? [];
  const moved = sleeve.contribution_eur > 0 || (sleeve.sale_eur ?? 0) > 0 || (sleeve.reinvest_eur ?? 0) > 0;
  return (
    <li className="space-y-2 py-3" data-testid={`sleeve-${sleeve.key}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="flex min-w-0 items-center gap-2 font-medium text-text-primary">
          {sleeve.unlocked ? (
            <CheckCircle2 className="h-4 w-4 shrink-0 text-success" aria-hidden />
          ) : (
            <Lock className="h-4 w-4 shrink-0 text-text-secondary" aria-hidden />
          )}
          <span>{sleeve.label}</span>
        </span>
        <span className="text-xs tabular-nums text-text-secondary">
          {formatCurrency(sleeve.current_eur, "EUR")} · now {pct(sleeve.current_pct)}
          {moved ? ` → ${pct(sleeve.after_pct)}` : ""} · target {pct(sleeve.target_pct)}
          {sleeve.key === "core" ? "" : ` · cap ${pct(sleeve.max_pct)}`}
          {sleeve.unlocked ? "" : " · locked"}
        </span>
      </div>
      <SleeveBar sleeve={sleeve} />
      {sleeve.contribution_eur > 0 && (
        <p className="text-sm text-text-primary">+{formatCurrency(sleeve.contribution_eur, "EUR")} this month</p>
      )}
      {(sleeve.sale_eur ?? 0) > 0 && (
        <p className="text-sm text-danger">−{formatCurrency(sleeve.sale_eur ?? 0, "EUR")} sold</p>
      )}
      {(sleeve.reinvest_eur ?? 0) > 0 && (
        <p className="text-sm text-text-primary">+{formatCurrency(sleeve.reinvest_eur ?? 0, "EUR")} from the sale</p>
      )}
      {sleeve.status && <p className="text-sm text-text-secondary">{sleeve.status}</p>}
      {(fund || lookThrough || positions.length > 0 || !sleeve.unlocked) && (
        <details className="group">
          <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-xs text-accent hover:underline">
            <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" /> Details
          </summary>
          <div className="mt-2 space-y-2">
            {fund && (fund.isin || sleeve.unlocked) && (
              <p className="text-sm text-text-secondary">
                <span className="font-medium text-text-primary">
                  {sleeve.unlocked ? "Suggested fund" : "Suggested fund once unlocked"}: {fund.isin ?? fund.name}.{" "}
                </span>
                {fund.why} <span className="text-text-muted">Rule, not a forecast.</span>
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
            {positions.length > 0 && (
              <ul className="space-y-1 text-sm">
                {positions.slice(0, 6).map((p) => (
                  <li key={p.isin + p.name} className="flex justify-between gap-3">
                    <span className="min-w-0 truncate text-text-secondary" title={p.isin}>{p.name}</span>
                    <span className="shrink-0 tabular-nums text-text-primary">{formatCurrency(p.value_eur, "EUR")}</span>
                  </li>
                ))}
                {positions.length > 6 && <li className="text-xs text-text-secondary">+{positions.length - 6} more</li>}
              </ul>
            )}
            {!sleeve.unlocked && (
              <Link to="/settings/profile#monthly-plan" className="text-xs text-accent underline-offset-2 hover:underline">
                Open in settings
              </Link>
            )}
          </div>
        </details>
      )}
    </li>
  );
}

