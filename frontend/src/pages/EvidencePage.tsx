import { formatDateTime } from "../lib/format";
import { useEffect, useState } from "react";
import { FlaskConical, Download, RefreshCw, AlertTriangle, ShieldCheck } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { LineChart } from "../components/charts/LineChart";
import { TrialsByContextCard } from "../components/evidence/TrialsByContextCard";
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
import { formatNumber } from "../lib/format";

/**
 * Evidence dashboard (ADR 0015): the global trial ledger and n_trials
 * resolver behind every Deflated Sharpe deflation, now counted per family
 * (ADR 0025): each gate deflates by the searches its candidate came from. Read-only.
 */
const GATE_LABELS: Record<string, string> = {
  evidence_gate: "Evidence gate (lab studies)",
  advisor: "Advisor champion",
  alphacrafter_miner: "AlphaCrafter factor admission",
  alphacrafter_trader: "AlphaCrafter dossier conviction",
  alphacrafter_tuning: "AlphaCrafter tuning",
};

export function EvidencePage({ headingLevel = 1 }: { headingLevel?: 1 | 2 } = {}) {
  const Heading = headingLevel === 1 ? "h1" : "h2";
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
        <Heading className="text-xl font-semibold flex items-center gap-2">
          <FlaskConical className="h-5 w-5" /> Evidence
        </Heading>
        <p className="text-sm text-text-secondary mt-1 max-w-2xl">
          The checks a strategy must pass before it gets any of your money. Documented factor
          premia are tested with a prior from the literature; mined signals must clear the
          Deflated Sharpe test against every trial of the search they were picked from, so each
          new idea tried in that search makes its bar higher.
        </p>
      </div>

      <EvidenceConsequences />

      <FactorPremiaCard />

      <IngredientAttributionCard />

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-text-secondary">All trials, every search</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">{summary.resolved}</div>
            <p className="mt-1 text-xs text-text-muted">
              The total over every family. No gate uses it: each counts only the searches its candidate came
              from (below).
            </p>
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
              independent tests lies between this and the count on the left.
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-text-secondary">Sharpe needed to beat luck</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">
              {hurdleAt(summary.resolved) != null ? formatNumber(hurdleAt(summary.resolved), { digits: 2 }) : "—"}
            </div>
            <p className="mt-1 text-xs text-text-muted">
              Yearly Sharpe the best of {summary.resolved} skill-less strategies reaches by chance over {years} years
              of daily data.
            </p>
          </CardContent>
        </Card>
      </div>

      {(summary.gates ?? []).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Trials behind each gate</CardTitle>
            <CardDescription>
              A gate deflates only by the trials its candidate was picked from. Where a trial stored its
              return or IC series, correlated trials count less (effective N).
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="divide-y divide-border text-sm">
              {(summary.gates ?? []).map((g) => (
                <li key={g.gate} className="flex flex-wrap items-baseline justify-between gap-2 py-2">
                  <span>
                    <span className="font-medium">{GATE_LABELS[g.gate] ?? g.gate}</span>
                    <span className="ml-2 font-mono text-xs text-text-muted">{g.families.join(", ")}</span>
                  </span>
                  <span className="tabular-nums">
                    N = {g.n_trials}
                    {g.n_effective !== g.n_trials && (
                      <span className="text-text-muted"> (effective {g.n_effective})</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

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
              yFormat={(v) => formatNumber(v, { digits: 2 })}
            />
          </CardContent>
        </Card>
      )}

      {Object.keys(summary.by_context).length + (summary.ac_trial_count ?? 0) > 0 && (
        <TrialsByContextCard
          byContext={summary.by_family ?? summary.by_context}
          acTrials={summary.by_family ? 0 : summary.ac_trial_count ?? 0}
          resolved={summary.resolved}
        />
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

