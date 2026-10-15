import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { formatCurrency, formatPercent } from "../../../lib/format";
import { FanChart } from "../../../components/charts/FanChart";
import { Skeleton } from "../../../components/ui/skeleton";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";
import { useLongHistoryProjection, useProjection } from "../hooks/useProjection";
import { useGoals } from "../hooks/useGoals";
import type { Goal, LongHistoryProjection } from "../../../lib/api";

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

/** The plan drawn from history instead of one model: before and after tax, and after a bad first decade. */
function LongHistoryCard({ data, years }: { data: LongHistoryProjection; years: number }) {
  if (!data.available || !data.terminal || !data.after_tax || !data.bad_start || !data.assumptions || !data.inputs) {
    return (
      <section className="rounded-md p-3 text-xs text-text-secondary bg-surface" data-testid="long-history">
        <h2 className="mb-1 text-sm font-semibold text-text-primary">Checked against history</h2>
        <p>{data.reason ?? "Not available."}</p>
      </section>
    );
  }
  const t = data.terminal;
  const a = data.assumptions;
  const mix = data.inputs.mix;
  return (
    <section className="rounded-md p-3 text-xs text-text-secondary bg-surface space-y-1" data-testid="long-history">
      <h2 className="text-sm font-semibold text-text-primary">Checked against history</h2>
      <p>
        The same plan replayed on {a.years_of_history.toLocaleString("de-DE")} years of {a.homes.length} euro-area
        countries since 1870, in blocks of about {a.mean_block_years} years, for your mix ({pct(mix.equity, 0)} equity,{" "}
        {pct(mix.bond, 0)} bonds, {pct(mix.cash, 0)} money market). Equity returns are cut by {pct(a.haircut, 0)} of their
        historical premium, as in ADR 0022.
      </p>
      <ul className="list-disc space-y-0.5 pl-4">
        <li>
          In {years} years: median {eur(t.median)}, bad case (5th percentile) {eur(t.p5)}; {pct(t.p_below_contributed, 0)} of
          paths end below the {eur(t.contributed)} you put in.
        </li>
        <li>
          After selling everything at the end and paying the flat tax on the nominal gain: median {eur(data.after_tax.median)}
          {" "}(tax about {eur(data.after_tax.median_tax)}), bad case {eur(data.after_tax.p5)}.
        </li>
        <li>
          If the first {data.bad_start.first_years} years go as badly as the worst tenth: median {eur(data.bad_start.median)},
          bad case {eur(data.bad_start.p5)}.
        </li>
        {data.all_countries && (
          <li>
            All {data.all_countries.homes} countries instead: median {eur(data.all_countries.median)}, bad case{" "}
            {eur(data.all_countries.p5)}.
          </li>
        )}
        {data.goal && (
          <li>
            Chance of the goal: {pct(data.goal.probability, 0)} before tax, {pct(data.goal.probability_after_tax, 0)} after.
          </li>
        )}
      </ul>
      <p className="text-text-muted">
        Source: {a.source}. Yearly steps; the Vorabpauschale counts against the final tax and is not shown separately.
        Estimate, not a forecast, tax advice or advice.
      </p>
    </section>
  );
}

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
  const history = useLongHistoryProjection({ years, goal: num(goal), contribution: num(contribution) });
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

        {history.data && <LongHistoryCard data={history.data} years={years} />}
      </div>
    </div>
  );
}
