import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { formatCurrency, formatPercent } from "../../../lib/format";
import { FanChart } from "../../../components/charts/FanChart";
import { Skeleton } from "../../../components/ui/skeleton";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";
import { useProjection } from "../hooks/useProjection";
import { useGoals } from "../hooks/useGoals";
import type { Goal } from "../../../lib/api";

const eur = (v?: number | null) => (v == null ? "-" : formatCurrency(v, "EUR", { digits: 0 }));
const pct = (v?: number | null, digits = 1) => formatPercent(v, { digits });
const num = (v: string): number | undefined => {
  const n = Number.parseFloat(v.replace(/\./g, "").replace(",", "."));
  return v.trim() === "" || !Number.isFinite(n) ? undefined : n;
};

/** Whole years from today to a goal's target date, at least 1 (undefined without a date). */
const yearsUntil = (date?: string | null): number | undefined => {
  if (!date) return undefined;
  const ms = Date.parse(date) - Date.now();
  return Number.isFinite(ms) ? Math.max(1, Math.round(ms / (365.25 * 24 * 3600 * 1000))) : undefined;
};

/**
 * Where the book could be in N years, in today's euros: a fan of 10,000
 * paths with monthly contributions, drift from a published long-run estimate
 * (Control Center › Profile › Projections), drift uncertainty and fat tails
 * (foundation/wealth_planner.py). The median is the central path; the mean
 * is higher and shown only for contrast.
 */
export function ScenariosView() {
  const [years, setYears] = useState(30);
  const [goal, setGoal] = useState("");
  const [contribution, setContribution] = useState("");
  const [realReturn, setRealReturn] = useState("");
  const [params, setParams] = useSearchParams();
  const goals = useGoals().data ?? [];
  const selectedGoal = params.get("goal");
  const pickGoal = (g: Goal | null) => {
    setParams((old) => {
      if (g) old.set("goal", g.id);
      else old.delete("goal");
      return old;
    });
    if (!g) return;
    if (g.target_amount) setGoal(String(g.target_amount));
    const y = yearsUntil(g.target_date);
    if (y) setYears(y);
    if (g.monthly_contribution) setContribution(String(g.monthly_contribution));
  };
  // A link such as /quantlab/scenarios?goal=<id> applies that goal once the list has loaded.
  const [applied, setApplied] = useState<string | null>(null);
  useEffect(() => {
    if (!selectedGoal || applied === selectedGoal || goals.length === 0) return;
    setApplied(selectedGoal);
    const g = goals.find((x) => x.id === selectedGoal);
    if (g) pickGoal(g);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedGoal, applied, goals]);
  const rr = num(realReturn);
  const proj = useProjection({
    years,
    goal: num(goal),
    contribution: num(contribution),
    realReturn: rr != null ? rr / 100 : undefined,
  });
  const d = proj.data;
  const fan = d?.fan ?? [];
  const g = d?.goal;

  const metrics: MetricItem[] = [
    { label: `Median in ${years} years`, value: eur(d?.terminal.median), hint: "today's euros" },
    { label: "Bad case (5th percentile)", value: eur(d?.terminal.p5), hint: "1 path in 20 ends lower" },
    { label: "Money you put in", value: eur(d?.terminal.contributed) },
    {
      label: "Chance of the goal",
      value: g ? pct(g.probability, 0) : "-",
      hint: g
        ? `${pct(g.probability_low_drift, 0)} – ${pct(g.probability_high_drift, 0)} if the return is 1 SE lower / higher`
        : "set a goal below",
    },
  ];

  return (
    <div className="space-y-0">
      <SummaryStrip metrics={metrics} />
      <div className="space-y-4 pt-4">
        <section className="flex flex-wrap items-end gap-3 rounded-md p-3 text-xs bg-surface">
          <label className="flex flex-col gap-1">
            <span className="text-text-muted">Years</span>
            <select className="rounded border border-line bg-surface px-2 py-1" value={years} onChange={(e) => setYears(Number(e.target.value))}>
              {[...new Set([5, 10, 15, 20, 25, 30, 40, years])].sort((a, b) => a - b).map((y) => (
                <option key={y} value={y}>{y}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-text-muted">Goal (€, today's money)</span>
            <input className="w-32 rounded border border-line bg-surface px-2 py-1" inputMode="decimal" value={goal}
              placeholder={d?.inputs.goal_eur ? String(d.inputs.goal_eur) : "none"} onChange={(e) => setGoal(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-text-muted">Monthly contribution (€)</span>
            <input className="w-28 rounded border border-line bg-surface px-2 py-1" inputMode="decimal" value={contribution}
              placeholder={d ? String(d.inputs.monthly_contribution_eur) : ""} onChange={(e) => setContribution(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-text-muted">Real return (% a year)</span>
            <input className="w-20 rounded border border-line bg-surface px-2 py-1" inputMode="decimal" value={realReturn}
              placeholder={d ? (100 * d.inputs.real_return).toFixed(1) : ""} onChange={(e) => setRealReturn(e.target.value)} />
          </label>
          <span className="text-text-muted">Empty fields use your book and your settings.</span>
        </section>

        {goals.length > 0 && (
          <section className="rounded-md p-3 text-xs bg-surface" data-testid="goal-presets">
            <h2 className="mb-1 text-sm font-semibold text-text-primary">Your goals</h2>
            <p className="mb-2 text-text-muted">Pick one to fill in the goal amount, the years and the monthly amount below.</p>
            <div className="flex flex-wrap gap-2">
              {goals.map((g) => (
                <button
                  key={g.id}
                  type="button"
                  aria-pressed={selectedGoal === g.id}
                  onClick={() => pickGoal(selectedGoal === g.id ? null : g)}
                  className={`rounded border px-2 py-1 text-left ${selectedGoal === g.id ? "border-accent text-accent" : "border-line"}`}
                >
                  <span className="font-medium">{g.title}</span>
                  {g.target_amount ? <span className="ml-2 text-text-muted">{eur(Number(g.target_amount))}</span> : null}
                  {g.target_date ? <span className="ml-2 text-text-muted">by {g.target_date}</span> : null}
                </button>
              ))}
            </div>
          </section>
        )}

        <section className="rounded-md p-3 bg-surface">
          <h2 className="mb-1 text-sm font-semibold text-text-primary">Your book in today's euros</h2>
          <p className="mb-2 text-[11px] text-text-muted">
            Shaded: the middle half and nine in ten of {d?.inputs.paths.toLocaleString("de-DE") ?? "10.000"} simulated paths.
            Dashed: the money you put in.
          </p>
          {proj.isLoading ? (
            <Skeleton className="h-80 w-full" />
          ) : (
            <FanChart
              ariaLabel="Projected real wealth by year"
              className="h-80"
              steps={fan.length}
              labels={fan.map((f) => f.year)}
              bands={fan.map((f) => ({ p05: f.p5, p25: f.p25, median: f.p50, p75: f.p75, p95: f.p95 }))}
              lines={[
                { name: "Money put in", data: fan.map((f) => f.contributed), dashed: true },
                ...(g ? [{ name: "Goal", data: fan.map(() => g.goal_eur), color: "#f59e0b", dashed: true }] : []),
              ]}
              xName="Years from now"
              yName="€ (today's money)"
              yFormat={(v) => eur(v)}
            />
          )}
        </section>

        {d && (
          <section className="rounded-md p-3 text-xs text-text-secondary bg-surface space-y-1">
            <h2 className="text-sm font-semibold text-text-primary">Assumptions</h2>
            <p>
              Return {pct(d.inputs.real_return)} a year after inflation, compounded ({d.assumptions.real_return_overridden
                ? "your input" : `${d.assumptions.real_return_source}, as of ${d.assumptions.real_return_as_of}`}).{" "}
              <Link to="/settings/profile#row-mc_cma_real_return" className="text-accent hover:underline">Change</Link>
            </p>
            <p>
              Each path draws its own long-run return: ± {pct(d.inputs.drift_standard_error)} (one standard error, as if
              estimated from {d.inputs.calibration_years} years of data), because nobody knows the future average.
            </p>
            <p>Volatility {pct(d.inputs.volatility)} a year: {d.assumptions.volatility_source}. Monthly shocks are {d.assumptions.fat_tails}.</p>
            <p>
              The average of all paths ({eur(d.terminal.mean)}) is higher than the median because a few paths do very well;
              the median is what to plan with.
              {g ? ` The chance of the goal is ±${pct(g.mc_standard_error, 1)} from sampling alone.` : ""}
            </p>
            <p className="text-text-muted">Estimate, not a forecast or advice. Taxes and fees are not deducted.</p>
          </section>
        )}
      </div>
    </div>
  );
}
