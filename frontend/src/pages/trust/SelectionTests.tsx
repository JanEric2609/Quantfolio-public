import type { TrustFamilyTest, TrustMandateTest, TrustNoiseCheck, TrustSelectionTest, TrustTimeGrid } from "../../lib/api";
import { Badge } from "../../components/ui/badge";
import { Card } from "../../components/ui/card";
import { fmtDay, fmtE, fmtPct, fmtPp, fmtYears } from "./trustFormat";

const SELECTION_STATE: Record<TrustSelectionTest["state"], { label: string; meaning: string }> = {
  too_early: { label: "Too early", meaning: "Fewer than three months of trading days: read the numbers as noise." },
  no_evidence: {
    label: "No evidence yet",
    meaning: "Neither a selection edge, nor harm, nor an edge too small to follow has shown so far.",
  },
  skill: { label: "Picks well", meaning: "The picks beat the stocks Discover could have picked instead." },
  harm: { label: "Harm", meaning: "The picks did worse than the stocks Discover could have picked instead." },
  not_worth_following: {
    label: "Not worth following",
    meaning: "The picks' edge over the alternatives is below what pays for trading them.",
  },
  not_valid: {
    label: "Not valid: serial correlation",
    meaning:
      "The daily values persist from one day to the next, so these daily tests can raise false alarms. No verdict here is acted on; the test moves to one observation per 21 days.",
  },
};

const MANDATE_STATE: Record<TrustMandateTest["state"], string> = {
  too_early: "Measuring its noise (first 63 days)",
  no_evidence: "Running",
  retire: "Retired: not worth running",
  retire_pending: "Shown not worth running; retires once its noise check passes at 252 days",
  not_valid: "Not valid: serial correlation (no verdict is acted on)",
};

/** One line on the §11 noise check: what it measured so far and what is next. */
export function noiseText(nc: TrustNoiseCheck): string {
  const done = nc.looks.filter((l) => l.vr != null);
  const measured = done.map((l) => `day ${l.day}: ${l.vr!.toFixed(2)} (limit ${l.cutoff})`).join(" · ");
  const next = nc.looks.find((l) => l.vr == null);
  if (nc.status === "failed") return `Noise check failed at day ${nc.failed_at_day}: ${measured}.`;
  if (nc.status === "passed") return `Noise check passed: ${measured}.`;
  const nextText = next ? `next look at ${next.day} trading days` : "";
  return nc.status === "pending"
    ? `Noise check: first look at ${next?.day ?? 63} trading days.`
    : `Noise check so far: ${measured}; ${nextText}.`;
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string | null }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="font-mono text-sm font-semibold tabular-nums text-text-primary">{value}</dd>
      {sub ? <dd className="text-[11px] text-text-muted">{sub}</dd> : null}
    </div>
  );
}

export function gridText(t: TrustTimeGrid): string {
  return t.grid
    .map((g) => {
      const edge = `${(g.edge_21d * 100).toLocaleString("en-US")} %`;
      const years = g.beyond_40y || g.q50_years == null ? "> 40 years" : `about ${fmtYears(g.q50_years)} years`;
      return `${edge}: ${years}`;
    })
    .join(" · ");
}

/** S and U: Discover's picks against the stocks it could have picked (ADR 0018 amendment of 2026-10-14). */
export function SelectionCard({ sel, f1 }: { sel: TrustSelectionTest; f1?: TrustFamilyTest }) {
  const meta = SELECTION_STATE[sel.state];
  const ci = sel.mean_ci_21d;
  const te = f1?.sd_daily != null ? f1.sd_daily * Math.sqrt(252) : null;
  return (
    <Card className="space-y-3 p-4" data-testid="family-S">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="space-y-0.5">
          <h3 className="text-sm font-semibold text-text-primary">
            Discover's picks vs the stocks it could have picked{" "}
            <Badge variant="outline" className="ml-1 align-middle text-[10px]">Primary</Badge>
          </h3>
          <p className="text-xs text-text-secondary">{sel.question}</p>
          <p className="text-xs text-text-secondary">{sel.futility_question}</p>
        </div>
        <Badge variant={sel.state === "harm" || sel.state === "not_worth_following" ? "danger" : "secondary"}>
          {meta.label}
          {sel.provisional ? " (provisional)" : ""}
        </Badge>
      </div>
      <p className="text-xs text-text-secondary">
        {meta.meaning}
        {sel.provisional ? " Provisional until the noise check's last look at 252 trading days." : ""}
      </p>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
        <Stat
          label="Trading days tested"
          value={sel.n_days.toLocaleString("en-US")}
          sub={sel.n_days ? `since ${fmtDay(sel.first_day ?? sel.start)}` : `starts ${fmtDay(sel.start)}`}
        />
        <Stat
          label="Picks minus alternatives"
          value={sel.mean_21d != null ? `${fmtPp(sel.mean_21d, 2)} / 21 days` : "—"}
          sub={ci ? `90 % interval ${fmtPp(ci[0], 2)} to ${fmtPp(ci[1], 2)}` : null}
        />
        <Stat
          label="Evidence (e-value)"
          value={`skill ${fmtE(sel.e_skill)} · harm ${fmtE(sel.e_harm)}`}
          sub={`one verdict needs ${sel.threshold}`}
        />
        <Stat
          label="Too small to follow"
          value={`e ${fmtE(sel.e_futility)}`}
          sub={`verdict at ${sel.futility_threshold}; margin ${fmtPp(sel.futility_delta_21d, 1)} / 21 days before costs`}
        />
      </dl>
      {sel.noise_check ? (
        <p className="text-[11px] text-text-muted" data-testid="selection-noise">
          {noiseText(sel.noise_check)} It measures whether the daily values persist (variance ratio over 21 days);
          above the limit the daily e-values are not valid.
        </p>
      ) : null}
      <p className="text-sm text-text-primary" data-testid="selection-grid">
        Median time to a verdict if the picks' true edge per 21 days is {gridText(sel.time_to_know)}.
      </p>
      <p className="text-[11px] text-text-muted">
        The comparison with MSCI World can only catch a large edge or harm. The question this page can answer in a few
        years is whether the picks are worth following. Alternatives are the same run's scored stocks in the pick's
        sector (all of them when the sector has fewer than three
        {sel.n_fallback_days ? `; ${sel.n_fallback_days} days used that fallback` : ""}).
        {te != null
          ? ` Risk budget: the basket moves ${fmtPct(te, 0)} a year apart from MSCI World (tracking error), mostly from its sector, style and currency tilt. That is reported, not tested.`
          : ""}
      </p>
    </Card>
  );
}

/** M: each mandate's futility test; a rejection retires it. */
export function MandatesCard({ mandates }: { mandates: TrustMandateTest[] }) {
  if (!mandates.length) return null;
  return (
    <Card className="space-y-2 p-4" data-testid="mandates">
      <h3 className="text-sm font-semibold text-text-primary">Are the mandates worth running?</h3>
      <p className="text-xs text-text-secondary">
        Each mandate must add at least {fmtPp(mandates[0].delta_21d, 1)} per 21 days over MSCI World after costs. When
        its test shows it adds less, it is retired: it stops trading and its value is still tracked.
      </p>
      <ul className="space-y-1 text-sm">
        {mandates.map((m) => (
          <li key={m.series} className="flex flex-wrap justify-between gap-2">
            <span className="text-text-primary">
              Mandate {m.mandate}: {MANDATE_STATE[m.state]}
            </span>
            <span className="font-mono text-xs tabular-nums text-text-secondary">
              {m.n_days} days · {m.mean_21d != null ? `${fmtPp(m.mean_21d, 2)} / 21 d` : "—"} · e {fmtE(m.e_futility)} of{" "}
              {m.threshold}
              {m.noise_check?.status === "failed" ? ` · noise check failed at day ${m.noise_check.failed_at_day}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}
