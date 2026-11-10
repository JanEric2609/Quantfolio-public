import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
/** Every row behind N, by context, on one scale: exact counts and their share of N. */
export function TrialsByContextCard({
  byContext,
  acTrials,
  resolved,
}: {
  byContext: Record<string, number>;
  acTrials: number;
  resolved: number;
}) {
  const rows = Object.entries(byContext).map(([ctx, n]) => ({
    key: ctx,
    label: ctx === "discover_legacy" ? "Discover factor scoring (until 28 Sep 2026)" : ctx,
    n,
  }));
  if (acTrials > 0) {
    rows.push({ key: "ac_trial_ledger", label: "Discover factor scoring (until 28 Sep 2026)", n: acTrials });
  }
  rows.sort((a, b) => b.n - a.n);
  const top = Math.max(...rows.map((r) => r.n), 1);
  const total = Math.max(resolved, 1);
  return (
    <Card data-testid="trials-by-context">
      <CardHeader>
        <CardTitle className="text-base">Trials by family</CardTitle>
        <CardDescription>
          Where search breadth has been spent. The rows add up to N = {resolved.toLocaleString("en-US")}, the total
          over every family; each gate deflates only by the families it draws from.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2">
          {rows.map((r) => (
            <li key={r.key} className="grid grid-cols-[minmax(0,12rem)_1fr_auto] items-center gap-3 text-sm">
              <span className="truncate text-text-secondary" title={r.label}>
                {r.label}
              </span>
              <span className="h-2 rounded-full bg-surface-3" aria-hidden>
                <span
                  className="block h-2 rounded-full bg-accent"
                  style={{ width: `${Math.max((r.n / top) * 100, 1)}%` }}
                />
              </span>
              <span className="font-mono text-xs tabular-nums text-text-primary">
                {r.n.toLocaleString("en-US")} · {((r.n / total) * 100).toFixed(r.n / total < 0.01 ? 1 : 0)} %
              </span>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
