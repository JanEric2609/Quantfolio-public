import { formatDateTime } from "../lib/format";
import { useEffect, useState } from "react";
import { FlaskConical, Download, RefreshCw, AlertTriangle, ShieldCheck } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { GaugeChart } from "../components/charts/GaugeChart";
import { LineChart } from "../components/charts/LineChart";
import { FactorPremiaCard } from "../components/evidence/FactorPremiaCard";
import { IngredientAttributionCard } from "../components/evidence/IngredientAttributionCard";
import { EvidenceConsequences } from "../components/evidence/EvidenceConsequences";
import { toast } from "sonner";
import {
  getNTrialsSummary,
  getTrialLedger,
  type NTrialsSummaryResponse,
  type TrialLedgerEntryDto,
} from "../lib/api";

/**
 * Evidence dashboard (ADR 0015): the global trial ledger and n_trials
 * resolver that back the graduation gate's Deflated Sharpe deflation — the
 * mechanism that makes the "silent period" un-gameable. Read-only.
 */
export function EvidencePage() {
  const [summary, setSummary] = useState<NTrialsSummaryResponse | null>(null);
  const [entries, setEntries] = useState<TrialLedgerEntryDto[]>([]);
  const [context, setContext] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  function load() {
    setLoading(true);
    setError(null);
    Promise.all([
      getNTrialsSummary(),
      getTrialLedger({ context: context || undefined, limit: 100 }),
    ])
      .then(([s, e]) => {
        // A partial payload used to throw in render (Object.keys of an
        // undefined by_context, .map on a non-array) and take the whole
        // Strategy-lab tab down with it.
        setSummary(s ? { ...s, by_context: s.by_context ?? {} } : null);
        setEntries(Array.isArray(e) ? e : []);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load evidence"))
      .finally(() => setLoading(false));
  }

  useEffect(load, [context]);

  function exportCSV() {
    if (entries.length === 0) return;
    const csv = [
      ["Registered at", "Context", "Trial key", "Metadata"].join(","),
      ...entries.map((e) =>
        [e.registered_at, e.context, e.trial_key, `"${JSON.stringify(e.metadata)}"`].join(",")
      ),
    ].join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `trial-ledger-${new Date().toISOString()}.csv`;
    a.click();
    toast.success(`Exported ${entries.length} row(s).`);
  }

  if (loading && !summary) {
    return <div className="p-6 text-text-secondary">Loading evidence…</div>;
  }
  if (error || !summary) {
    return (
      <div className="p-6">
        <div className="flex items-center gap-2 text-danger">
          <AlertTriangle className="h-4 w-4" /> {error ?? "No data"}
        </div>
        <Button variant="outline" className="mt-3" onClick={load}>
          <RefreshCw className="h-4 w-4 mr-2" /> Retry
        </Button>
      </div>
    );
  }

  const curve = summary.hurdle_curve ?? [];
  const hurdleAt = (n: number) => curve.find((p) => p.n_trials === n)?.sharpe_hurdle;
  const years = summary.hurdle_observations ? Math.round(summary.hurdle_observations / 252) : 5;

  return (
    <div className="p-6 space-y-6 max-w-4xl">
      <div>
        <h1 className="text-xl font-semibold flex items-center gap-2">
          <FlaskConical className="h-5 w-5" /> Evidence
        </h1>
        <p className="text-sm text-text-secondary mt-1 max-w-2xl">
          The checks a strategy must pass before it gets any of your money. Documented factor
          premia are tested with a prior from the literature; mined signals must clear the
          Deflated Sharpe test against every trial ever run, so each new idea tried anywhere
          makes the bar higher for all of them.
        </p>
      </div>

      <EvidenceConsequences />

      <FactorPremiaCard />

      <IngredientAttributionCard />

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-text-secondary">Trials counted against each test</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">{summary.resolved}</div>
            <p className="mt-1 text-xs text-text-muted">Every ledger row, across all contexts.</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-text-secondary flex items-center gap-1">
              <ShieldCheck className="h-3.5 w-3.5" /> Independent searches
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">{summary.effective ?? "—"}</div>
            <p className="mt-1 text-xs text-text-muted">
              Configurations of one run share data and signal, so they count once here. The true number of
              independent tests lies between this and the count on the left; the gates use the higher one.
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-text-secondary">Sharpe needed to beat luck</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">
              {hurdleAt(summary.resolved) != null ? hurdleAt(summary.resolved)!.toFixed(2) : "—"}
            </div>
            <p className="mt-1 text-xs text-text-muted">
              Yearly Sharpe the best of {summary.resolved} skill-less strategies reaches by chance over {years} years
              of daily data.
            </p>
          </CardContent>
        </Card>
      </div>

      {curve.length > 1 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">How the bar rises with the number of trials</CardTitle>
            <CardDescription>
              Expected best Sharpe of N strategies with no skill, {years} years of daily returns (False Strategy
              Theorem, Bailey and López de Prado 2014). A strategy below the line at the ledger&apos;s N is
              indistinguishable from the luckiest of N coin flips.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <LineChart
              ariaLabel="Sharpe hurdle against the number of trials"
              className="h-64"
              xType="category"
              series={[{ name: "Sharpe hurdle", data: curve.map((p) => [String(p.n_trials), p.sharpe_hurdle]) }]}
              xName="Number of trials (N)"
              yName="Yearly Sharpe hurdle"
              yFormat={(v) => v.toFixed(2)}
            />
          </CardContent>
        </Card>
      )}

      {Object.keys(summary.by_context).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Trials by context</CardTitle>
            <CardDescription>Where search breadth has actually been spent.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-4">
            {Object.entries(summary.by_context).map(([ctx, count]) => (
              <div key={ctx} className="flex flex-col items-center gap-1 w-36">
                <GaugeChart
                  value={count}
                  max={Math.max(count, 10)}
                  label={ctx}
                  height={120}
                  ariaLabel={`Trial count for ${ctx}`}
                />
                <span className="text-xs text-text-secondary">{ctx}</span>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <CardTitle className="text-base">Trial ledger</CardTitle>
              <CardDescription>Most recent 100 entries. Idempotent per (context, trial key).</CardDescription>
            </div>
            <Button size="sm" variant="outline" onClick={exportCSV}>
              <Download className="h-4 w-4 mr-2" /> Export CSV
            </Button>
          </div>
          <Input
            placeholder="Filter by context (e.g. alphacrafter_tuning)"
            value={context}
            onChange={(e) => setContext(e.target.value)}
            className="mt-3"
          />
        </CardHeader>
        <CardContent>
          {entries.length === 0 ? (
            <p className="text-sm text-text-secondary">No ledger entries yet.</p>
          ) : (
            <div className="space-y-2">
              {entries.map((e) => (
                <div key={e.id} className="rounded-md border border-border p-2 text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono">{e.context}</span>
                    <span className="text-text-secondary">
                      {formatDateTime(e.registered_at)}
                    </span>
                  </div>
                  <div className="text-text-secondary mt-0.5">{e.trial_key}</div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
