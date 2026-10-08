import type { TrustFactorNeutral, TrustPairedComparison } from "../../lib/api";
import { Badge } from "../../components/ui/badge";
import { Card } from "../../components/ui/card";
import { LineChart } from "../../components/charts/LineChart";
import { fmtDay, fmtPct, fmtPp } from "./trustFormat";

function Descriptive() {
  return (
    <Badge variant="secondary" className="ml-1 align-middle text-[10px]">
      Descriptive
    </Badge>
  );
}

/** Advisor against Discover's picks on common days (ADR 0018 Phase 3). Never a verdict. */
export function PairedComparisonCard({ paired }: { paired: TrustPairedComparison }) {
  const ci = paired.mean_ci_21d;
  return (
    <Card className="space-y-2 p-4" data-testid="paired-comparison">
      <h3 className="text-sm font-semibold text-text-primary">
        Advisor vs Discover's picks <Descriptive />
      </h3>
      <p className="text-xs text-text-secondary">{paired.question}</p>
      {paired.n_days === 0 ? (
        <p className="text-sm text-text-primary">
          No day yet on which both had open calls (counting from {fmtDay(paired.start)}).
        </p>
      ) : (
        <>
          <p className="text-sm text-text-primary">
            On {paired.n_days.toLocaleString("en-US")} common trading days the advisor was{" "}
            <span className="font-mono font-semibold">{fmtPp(paired.mean_21d, 2)}</span> per 21 days against the
            picks
            {ci ? ` (90 % interval ${fmtPp(ci[0], 2)} to ${fmtPp(ci[1], 2)})` : ""}, ahead on{" "}
            {fmtPct(paired.share_advisor_ahead)} of days.
          </p>
          {paired.path.length >= 2 ? (
            <LineChart
              ariaLabel="Advisor minus Discover's picks, cumulative"
              height={140}
              yFormat={(v) => fmtPp(v, 1)}
              series={[{ name: "Advisor − picks", data: paired.path.map((p) => [p.day, p.cumulative] as [string, number]) }]}
            />
          ) : null}
        </>
      )}
      <p className="text-[11px] text-text-muted">
        Both are measured against the same ETF, so it cancels. There is no e-value and no threshold here: the advisor is
        a paper-only research loop, and the headline stays with Discover's picks.
        {paired.enough_days ? "" : " Fewer than 63 common days: read it as noise."}
        {paired.n_days_advisor_only || paired.n_days_ideas_only
          ? ` Days with only one side open: advisor ${paired.n_days_advisor_only}, picks ${paired.n_days_ideas_only}.`
          : ""}
      </p>
    </Card>
  );
}

const DECISION_TEXT: Record<"build" | "drop" | "neither", string> = {
  build: "explained enough to build the row",
  drop: "explained too little: the idea is dropped",
  neither: "in between: not built, and a new factor set would need a new pre-registration",
};

/** ADR 0018 §8: the factor study and, only if it said "build", the factor-neutral row. */
export function FactorNeutralCard({ fn }: { fn: TrustFactorNeutral }) {
  const { study, row, gate } = fn;
  const ci = row?.mean_ci_21d;
  return (
    <Card className="space-y-2 p-4" data-testid="factor-neutral">
      <h3 className="text-sm font-semibold text-text-primary">
        Picks after removing factor moves <Descriptive />
      </h3>
      <p className="text-xs text-text-secondary">
        How much of the picks' moves the market, their sectors, the dollar, Europe against the world and the style
        factors (value, momentum, quality, low volatility, size) explain. A study on the back-filled baskets decides once whether this row is
        built: at {fmtPct(gate.build, 0)} out-of-sample R² or more it is, below {fmtPct(gate.drop, 0)} the idea is
        dropped.
      </p>
      {study.status === "waiting" ? (
        <p className="text-sm text-text-primary">
          The study has not run yet: it needs {gate.min_fit_days + gate.min_oos_days} trading days of back-filled
          baskets ({study.n_backfilled_picks ?? 0} back-filled picks so far).
        </p>
      ) : (
        <p className="text-sm text-text-primary">
          Study (spec v{study.spec_version}, {fmtDay(study.computed_at)}): out-of-sample R²{" "}
          <span className="font-mono font-semibold">{study.oos_r2 != null ? study.oos_r2.toFixed(2) : "—"}</span> over{" "}
          {study.n_oos_days} days, {study.decision ? DECISION_TEXT[study.decision] : ""}.
        </p>
      )}
      {row ? (
        <p className="text-sm text-text-primary">
          {row.n_days
            ? `Mean after factors: ${fmtPp(row.mean_21d, 2)} per 21 days over ${row.n_days} days${ci ? ` (90 % interval ${fmtPp(ci[0], 2)} to ${fmtPp(ci[1], 2)})` : ""}.`
            : "No live day recorded yet."}
        </p>
      ) : null}
      <p className="text-[11px] text-text-muted">
        Secondary: never part of the picks' test. Exposures come only from days before each one.
      </p>
    </Card>
  );
}
