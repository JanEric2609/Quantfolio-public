import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, getCrashGuard, getLadderState, getMonthlyPlan, type DiscoverRunSummary, type DiscoverRunDetail } from "../../lib/api";
import { formatCurrency, formatPercentPoints } from "../../lib/format";
import { useProjection } from "../quantlab/hooks/useProjection";
import { useTrustVerdict } from "../trust/useTrust";
import { Feedback, Station } from "./Station";
import type { LearnState } from "./progress";

interface StationProps {
  state: LearnState;
  onAnswer: (id: string, value: number) => void;
}

const pctText = (v: number) => formatPercentPoints(v, { digits: v >= 10 || v === 0 ? 0 : 1 });

/** Slider that reveals the answer only after it was moved: predict first, then see. */
function Guess({ id, state, onAnswer, min, max, step, unit, label }: StationProps & {
  id: string; min: number; max: number; step: number; unit: string; label: string;
}) {
  const value = state.answers[id] ?? null;
  return (
    <label className="block text-sm">
      <span className="text-text-secondary">{label}</span>
      <input
        type="range" min={min} max={max} step={step} value={value ?? min}
        onChange={(e) => onAnswer(id, Number(e.target.value))}
        className="mt-2 w-full" aria-label={label}
      />
      <span className="text-text-primary" data-testid={`${id}-guess`}>{value == null ? "Move the slider to answer" : `${value}${unit}`}</span>
    </label>
  );
}

/** 1. Where my money is. */
export function WhereMyMoney(p: StationProps) {
  const plan = useQuery({ queryKey: ["plan", "month"], queryFn: getMonthlyPlan });
  const core = plan.data?.sleeves?.find((s) => s.key === "core");
  const empty = plan.isLoading ? "Loading." : !plan.data || !plan.data.has_holdings || !core ? "No synced holdings yet." : null;
  const guess = p.state.answers["where"];
  return (
    <Station id="where" title="Where my money is" question="What share of your book is in the core fund?"
      source="This month" sourceTo="/plan" empty={empty}>
      <Guess id="where" {...p} min={0} max={100} step={5} unit=" %" label="Your guess for the core share" />
      <Feedback show={guess != null && !!core}>
        It is {core ? pctText(core.current_pct) : ""} of {plan.data ? formatCurrency(plan.data.book_eur, "EUR") : ""}.
        {guess != null && core ? ` You were ${Math.abs(guess - core.current_pct) <= 5 ? "close" : `${Math.round(Math.abs(guess - core.current_pct))} points off`}.` : ""}
      </Feedback>
    </Station>
  );
}

/** 2. What the monthly plan does with a contribution. */
export function MonthlyPlanStation(p: StationProps) {
  const plan = useQuery({ queryKey: ["plan", "month"], queryFn: getMonthlyPlan });
  const sleeves = plan.data?.sleeves ?? [];
  const contribution = p.state.answers["plan"] ?? plan.data?.contribution_eur ?? 0;
  const empty = plan.isLoading ? "Loading." : sleeves.length === 0 ? "No plan yet." : null;
  const split = useMemo(() => {
    const open = sleeves.filter((s) => s.key !== "core" && s.unlocked && s.target_pct > 0);
    const side = open.reduce((acc, s) => acc + s.target_pct, 0);
    return [
      ...open.map((s) => ({ label: s.label, eur: (contribution * s.target_pct) / 100 })),
      { label: "Core", eur: contribution * (1 - side / 100) },
    ];
  }, [sleeves, contribution]);
  return (
    <Station id="plan" title="What the monthly plan does" question="Move the monthly amount: where does the plan send it?"
      source="This month" sourceTo="/plan" empty={empty}>
      <Guess id="plan" {...p} min={0} max={3000} step={50} unit=" €" label="Monthly amount" />
      <Feedback show={p.state.answers["plan"] != null}>
        {split.map((s) => `${s.label} ${formatCurrency(s.eur, "EUR")}`).join(" · ")}. New money goes to whatever is below its target; locked parts get nothing yet.
      </Feedback>
    </Station>
  );
}

/** 3. The long-run range. */
export function LongRun(p: StationProps) {
  const years = p.state.answers["years"] ?? 20;
  const monthly = p.state.answers["monthly"] ?? 100;
  const proj = useProjection({ years, contribution: monthly });
  const t = proj.data?.terminal;
  const empty = proj.isError ? "The projection is not available." : null;
  return (
    <Station id="longrun" title="The long-run range" question="How wide is the range of outcomes? Move the years and the monthly amount."
      source="Quant Lab → Scenarios" sourceTo="/quantlab/scenarios" empty={empty}>
      <Guess id="years" {...p} min={5} max={40} step={5} unit=" years" label="Years" />
      <Guess id="monthly" {...p} min={0} max={1000} step={50} unit=" € a month" label="Monthly contribution" />
      <Feedback show={!!t}>
        In {years} years the median is {t ? formatCurrency(t.median, "EUR", { digits: 0 }) : ""} in today&apos;s money and the bad case
        (1 path in 20) {t ? formatCurrency(t.p5, "EUR", { digits: 0 }) : ""}. Of the {t ? formatCurrency(t.contributed, "EUR", { digits: 0 }) : ""} paid in,
        {" "}{t?.start_value != null ? formatCurrency(t.start_value, "EUR", { digits: 0 }) : "today's portfolio"} is what you hold today.
      </Feedback>
    </Station>
  );
}

const SIGNAL_LABEL: Record<string, string> = {
  momentum: "Momentum", portfolio: "Portfolio fit", fundamentals: "Fundamentals", risk: "Risk", benchmark: "Versus benchmark",
  analyst: "Analysts", sentiment: "News tone", ic_icir: "Factor exposure", ml_signal: "ML signal",
  estimate_revision: "Estimate revisions", insider_signal: "Insiders",
};

/** Composite of one stock for weights the reader sets; the rank among the shortlist is recomputed in the browser. */
export function compositeFor(inputs: { signal: string; score: number }[], weights: Record<string, number>): number {
  const total = inputs.reduce((acc, i) => acc + (weights[i.signal] ?? 1), 0);
  return total > 0 ? inputs.reduce((acc, i) => acc + i.score * (weights[i.signal] ?? 1), 0) / total : 0;
}

/** 4. Why a stock is on the list. */
export function WhyOnList(p: StationProps) {
  const runs = useQuery({ queryKey: ["discover-runs"], queryFn: () => api<DiscoverRunSummary[]>("/api/discover/runs") });
  const latest = runs.data?.find((r) => r.status === "completed");
  const detail = useQuery({
    queryKey: ["discover-run", latest?.id],
    queryFn: () => api<DiscoverRunDetail>(`/api/discover/runs/${latest!.id}`),
    enabled: !!latest,
  });
  const rows = (detail.data?.shortlisted ?? [])
    .map((c) => ({ symbol: c.symbol, inputs: ((c.scores as { composite_inputs?: { signal: string; score: number; weight: number }[] } | undefined)?.composite_inputs ?? []) }))
    .filter((r) => r.inputs.length > 0);
  const signals = rows[0]?.inputs.map((i) => i.signal) ?? [];
  const [weights, setWeights] = useState<Record<string, number>>({});
  const ranked = rows.map((r) => ({ symbol: r.symbol, c: compositeFor(r.inputs, weights) })).sort((a, b) => b.c - a.c);
  const empty = runs.isLoading || detail.isLoading ? "Loading." : rows.length === 0 ? "No finished Discover run with stored scores yet." : null;
  const first = ranked[0]?.symbol;
  return (
    <Station id="why" title="Why a stock is on the list" question={`Give each signal a weight and see who ranks first among the ${rows.length} names on the list.`}
      source="Discover" sourceTo="/discover" empty={empty}>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {signals.map((s) => (
          <label key={s} className="text-xs text-text-secondary">
            {SIGNAL_LABEL[s] ?? s}
            <input type="range" min={0} max={3} step={0.5} value={weights[s] ?? 1} aria-label={SIGNAL_LABEL[s] ?? s}
              onChange={(e) => setWeights({ ...weights, [s]: Number(e.target.value) })} className="w-full" />
          </label>
        ))}
      </div>
      <Feedback show={!!first}>With these weights {first} ranks first; the list&apos;s own weights are shown on each name&apos;s card in Discover.</Feedback>
    </Station>
  );
}

/** 5. Shadow or recommendation: the hit rate against a coin flip. */
export function HitRate(p: StationProps) {
  const verdict = useTrustVerdict();
  const ideas = verdict.data?.types.find((t) => t.type === "ideas") ?? verdict.data?.types[0];
  const empty = verdict.isLoading ? "Loading." : !ideas || ideas.hit_rate == null ? "No resolved calls yet." : null;
  return (
    <Station id="hit" title="Shadow or recommendation?" question="Out of 100 resolved picks, how many beat MSCI World?"
      source="Can I trust it?" sourceTo="/trust" empty={empty}>
      <Guess id="hit" {...p} min={0} max={100} step={5} unit=" of 100" label="Your guess" />
      <Feedback show={p.state.answers["hit"] != null && !!ideas?.hit_rate}>
        {ideas?.hit_rate != null ? `${Math.round(ideas.hit_rate * 100)} of 100 did (${ideas.hits} of ${ideas.n_calls ?? ideas.n} resolved). A coin flip gives 50, so the list is a shadow until the evidence says more.` : ""}
      </Feedback>
    </Station>
  );
}

/** 6. The capital ladder. */
export function Ladder(_p: StationProps) {
  const ladder = useQuery({ queryKey: ["graduation", "ladder"], queryFn: getLadderState });
  const c = ladder.data?.candidates?.[0];
  const empty = ladder.isLoading ? "Loading." : !c ? "No candidate on the ladder yet." : null;
  return (
    <Station id="ladder" title="The capital ladder" question="How much evidence does a stock-picking strategy need before it gets real money?"
      source="Capital ladder" sourceTo="/graduation" empty={empty}>
      <Feedback show={!!c}>
        {c ? `${c.stage_name}: evidence ${c.e_skill.toFixed(1)} against ${c.thresholds.earned} needed for the next stage, after ${c.days_of_evidence} days. ${c.next_step}` : ""}
      </Feedback>
    </Station>
  );
}

/** 7. Crash guard. */
export function CrashGuard(p: StationProps) {
  const guard = useQuery({ queryKey: ["plan", "crash-guard"], queryFn: getCrashGuard });
  const m = guard.data?.market;
  const empty = guard.isLoading ? "Loading." : !m ? "No market data yet." : null;
  return (
    <Station id="crash" title="Crash guard" question="How far below its high is the world market today?"
      source="Home" sourceTo="/" empty={empty}>
      <Guess id="crash" {...p} min={0} max={50} step={1} unit=" % below" label="Your guess" />
      <Feedback show={p.state.answers["crash"] != null && !!m}>
        {m ? `${Math.round(m.drawdown * 100)} % below its high: ${m.level_label}. ${guard.data?.plan_note ? `Your own rule: ${guard.data.plan_note}` : "You have not written a rule for a fall yet."}` : ""}
      </Feedback>
    </Station>
  );
}

/** Tax on one sale of an equity fund, as an estimate: 25 % + solidarity surcharge on the gain after the 30 % partial exemption. */
export function saleTaxEstimate(proceeds: number, cost: number): number {
  const gain = Math.max(0, proceeds - cost);
  return gain * 0.7 * 0.25 * 1.055;
}

/** 8. Tax. */
export function TaxStation(p: StationProps) {
  const proceeds = p.state.answers["sale"] ?? 10000;
  const cost = 8000;
  const tax = saleTaxEstimate(proceeds, cost);
  return (
    <Station id="tax" title="What would one sale cost?" question={`You bought an equity fund for ${formatCurrency(cost, "EUR", { digits: 0 })}. What does a sale cost in tax?`}
      source="Tax cockpit" sourceTo="/tax">
      <Guess id="sale" {...p} min={8000} max={20000} step={500} unit=" € proceeds" label="Sale proceeds" />
      <Feedback show={p.state.answers["sale"] != null}>
        About {formatCurrency(tax, "EUR", { digits: 0 })}: 25 % plus solidarity surcharge on the gain, after the 30 % partial exemption for equity funds
        and before your saver&apos;s allowance. <strong>Estimate, not tax advice</strong>; your broker&apos;s statement is the source of truth.
      </Feedback>
    </Station>
  );
}
