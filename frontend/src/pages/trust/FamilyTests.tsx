import type { TrustDailyTests, TrustFamilyTest } from "../../lib/api";
import { Badge } from "../../components/ui/badge";
import { Card } from "../../components/ui/card";
import { LineChart } from "../../components/charts/LineChart";
import { EvidenceChip } from "./EvidenceChip";
import { FactorNeutralCard, PairedComparisonCard } from "./SecondaryRows";
import { STATE_META, assumptionLabel, fmtDay, fmtE, fmtPct, fmtPp, fmtYears, timeToKnowText } from "./trustFormat";

const TITLES: Record<TrustFamilyTest["family"], string> = {
  F1: "Discover's picks vs your ETF",
  F2: "Discover's ranking of every scored stock",
  F3: "Advisor's calls vs your ETF",
};

function Stat({ label, value, sub }: { label: string; value: string; sub?: string | null }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="font-mono text-sm font-semibold tabular-nums text-text-primary">{value}</dd>
      {sub ? <dd className="text-[11px] text-text-muted">{sub}</dd> : null}
    </div>
  );
}

/** The e-value path on a log scale against the bar one verdict needs. */
function EPath({ family }: { family: TrustFamilyTest }) {
  if (family.path.length < 2) return null;
  const bar = family.path.map((p) => [p.day, family.threshold] as [string, number]);
  return (
    <LineChart
      ariaLabel={`${TITLES[family.family]}: evidence over time`}
      height={160}
      yLog
      yFormat={(v) => fmtE(v)}
      series={[
        { name: "Skill", data: family.path.map((p) => [p.day, p.e_skill] as [string, number]) },
        { name: "Harm", data: family.path.map((p) => [p.day, p.e_harm] as [string, number]) },
        { name: `Bar (${family.threshold})`, data: bar },
      ]}
    />
  );
}

function Posterior({ family }: { family: TrustFamilyTest }) {
  const post = family.posterior;
  if (!post) return null;
  const tau = fmtPct(post.tau_21d, 2).replace(" %", "");
  const unit = family.series === "ranking" ? "long-short return" : "edge";
  return (
    <div className="space-y-1 rounded-md bg-surface-2 p-3" data-testid={`posterior-${family.family}`}>
      <p className="text-sm text-text-primary">
        Chance the {unit} is positive: <span className="font-mono font-semibold">{fmtPct(post.p_positive)}</span>{" "}
        <span className="text-text-muted">
          (90 % credible range {fmtPp(post.ci_low_21d, 2)} to {fmtPp(post.ci_high_21d, 2)} per 21 days; sceptical prior,
          ±{tau} % per 21 days)
        </span>
      </p>
      <p className="text-[11px] text-text-muted">
        This number cannot declare skill: with no edge at all it passes 95 % at some point in 8–22 % of simulated
        histories. Only the test can. With a different prior:{" "}
        {post.sensitivity.map((s) => `±${Math.round(s.tau_21d * 10000) / 100} % → ${fmtPct(s.p_positive)}`).join(" · ")}.
      </p>
    </div>
  );
}

function TimeToKnow({ family }: { family: TrustFamilyTest }) {
  const t = family.time_to_know;
  const assumption = assumptionLabel(t.assumption_unit, t.assumption);
  const noise =
    t.assumption_unit === "rank_ic"
      ? `IC noise ${t.ic_sd ?? "—"} per run`
      : t.basket_sd_21d != null
        ? `${Math.round(t.basket_sd_21d * 100)} % basket noise per 21 days`
        : `the measured daily noise (${fmtPct(t.sd_daily, 2)})`;
  return (
    <div className="space-y-1">
      <p className="text-sm text-text-primary">
        If the true {t.assumption_unit === "rank_ic" ? "ranking skill" : "edge"} is {assumption}, a verdict most likely takes{" "}
        <span className="font-semibold">{timeToKnowText(t)}</span> from the start.
      </p>
      <p className="text-[11px] text-text-muted">
        Simulated with {noise}{t.sd_measured ? " (measured)" : " (assumed)"}. Chance of a verdict within{" "}
        {t.p_within.map((w) => `${w.years} years: ${fmtPct(w.p)}`).join(", ")}. Median at other values:{" "}
        {t.grid.map((g) => `${assumptionLabel(t.assumption_unit, g.assumption)} → ${g.q50_years != null ? `${fmtYears(g.q50_years)} y` : "never within the simulation"}`).join(" · ")}.
        If there is no edge, no verdict ever comes; that is the test working.
      </p>
    </div>
  );
}

export function FamilyCard({ family, start }: { family: TrustFamilyTest; start: string }) {
  const chip = { state: family.state, n: family.n_days, n_needed: null, benchmarked: true, type: "ideas" as const };
  const ci = family.mean_ci_21d;
  return (
    <Card className="space-y-3 p-4" data-testid={`family-${family.family}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="space-y-0.5">
          <h3 className="text-sm font-semibold text-text-primary">
            {TITLES[family.family]}{" "}
            <Badge variant={family.role === "primary" ? "outline" : "secondary"} className="ml-1 align-middle text-[10px]">
              {family.role === "primary" ? "Primary" : "Secondary"}
            </Badge>
          </h3>
          <p className="text-xs text-text-secondary">{family.question}</p>
        </div>
        <EvidenceChip type={chip} />
      </div>
      <p className="text-xs text-text-secondary">{STATE_META[family.state].meaning}</p>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
        <Stat
          label="Trading days tested"
          value={`${family.n_days.toLocaleString("en-US")}`}
          sub={family.n_days ? `since ${fmtDay(family.first_day ?? start)}` : `starts ${fmtDay(start)}`}
        />
        <Stat
          label={family.series === "ranking" ? "Long-short return" : "Mean vs your ETF"}
          value={family.mean_21d != null ? `${fmtPp(family.mean_21d, 2)} / 21 days` : "—"}
          sub={ci ? `90 % interval ${fmtPp(ci[0], 2)} to ${fmtPp(ci[1], 2)}` : null}
        />
        <Stat
          label="Evidence (e-value)"
          value={`skill ${fmtE(family.e_skill)} · harm ${fmtE(family.e_harm)}`}
          sub={`one verdict needs ${family.threshold} (skill and harm, 5 % false discoveries)`}
        />
        <Stat
          label="Open on an average day"
          value={family.mean_open != null ? family.mean_open.toFixed(1) : "—"}
          sub={family.series === "ranking" ? "scored runs (cohorts)" : "calls"}
        />
      </dl>

      <EPath family={family} />
      <Posterior family={family} />
      <TimeToKnow family={family} />

      <p className="text-[11px] text-text-muted">
        {family.pre_registration.n_days
          ? `${family.pre_registration.n_days} trading days before ${fmtDay(start)} are shown for context only (before pre-registration); they never enter the e-value. `
          : ""}
        {family.n_stale_days ? `${family.n_stale_days} days used a carried-forward price for at least one name. ` : ""}
        {family.n_clipped ? `${family.n_clipped} days were beyond the ±5 % clip. ` : ""}
        {family.rho1 != null
          ? `Day-to-day correlation of the first three months: ${family.rho1.toFixed(2)}${family.rho1_flag ? " (above 0.15: noted, the test stays valid)" : ""}.`
          : ""}
      </p>
    </Card>
  );
}

/** The pre-registered calendar-time tests (ADR 0018): one card per family. */
export function FamilyTests({ tests }: { tests: TrustDailyTests }) {
  return (
    <section aria-labelledby="trust-tests" className="space-y-3">
      <div>
        <h2 id="trust-tests" className="text-sm font-semibold text-text-primary">
          The pre-registered tests
        </h2>
        <p className="text-xs text-text-secondary">
          Fixed in advance (ADR 0018) before the results they judge. Each trading day the open calls form one portfolio
          held as issued; its return against your ETF that day is one observation, recorded once and never rewritten.
          Days count from {fmtDay(tests.start)}.
        </p>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        {tests.families.map((f) => (
          <FamilyCard key={f.family} family={f} start={tests.start} />
        ))}
        {tests.factor_neutral ? <FactorNeutralCard fn={tests.factor_neutral} /> : null}
        {tests.paired ? <PairedComparisonCard paired={tests.paired} /> : null}
      </div>
    </section>
  );
}
