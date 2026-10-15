import type { TrustPairedComparison } from "../../lib/api";
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
