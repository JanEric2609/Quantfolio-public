import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { FlaskConical, Layers, Lock, Timer } from "lucide-react";
import { Badge } from "../components/ui/badge";
import { Card } from "../components/ui/card";
import { Disclosure } from "../components/composed/Disclosure";
import { PageHeader } from "../components/composed/PageHeader";
import { getAlgoOverview, type AlgoOverview } from "../lib/api";

function Tile({ icon, label, value, sub }: { icon: React.ReactNode; label: string; value: string; sub: string }) {
  return (
    <Card className="space-y-1 p-4">
      <p className="flex items-center gap-1.5 text-xs uppercase tracking-wide text-text-muted">{icon}{label}</p>
      <p className="font-mono text-2xl font-semibold tabular-nums text-text-primary">{value}</p>
      <p className="text-xs text-text-secondary">{sub}</p>
    </Card>
  );
}

export function AlgoPage() {
  const [data, setData] = useState<AlgoOverview | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    getAlgoOverview().then(setData).catch(() => setError(true));
  }, []);

  const maxYears = data ? Math.max(1, ...data.years_to_detect.map((r) => r.years)) : 1;

  return (
    <div className="space-y-5">
      <PageHeader title="Algo trading (research)" subtitle="Slow, low-cost rules on paper. Nothing here is funded or ordered." />
      {error && <p role="alert" className="text-sm text-red-600">The overview could not be loaded.</p>}
      {data && (
        <>
          <Card className="flex flex-wrap items-center gap-3 border-l-4 border-l-amber-500 p-5">
            <Badge variant="outline" className="gap-1.5"><Lock className="h-3 w-3" aria-hidden="true" />Paper only</Badge>
            <p className="min-w-0 flex-1 text-sm text-text-primary">
              {data.funded_candidates === 0 ? "No rule has earned real money yet." : `${data.funded_candidates} rule(s) funded.`}{" "}
              <span className="text-text-secondary">{data.note}</span>
            </p>
          </Card>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Tile icon={<Lock className="h-3.5 w-3.5" aria-hidden="true" />} label="Funded" value={String(data.funded_candidates)} sub="rules trading real money" />
            <Tile icon={<Layers className="h-3.5 w-3.5" aria-hidden="true" />} label="Draft cards" value={`${data.cards.length} / ${data.max_arms}`} sub="risk overlays, judged on drawdown" />
            <Tile icon={<FlaskConical className="h-3.5 w-3.5" aria-hidden="true" />} label="Paper cells" value={String(data.paper_cells.length)} sub="collecting evidence vs costs and a placebo" />
            <Tile
              icon={<Timer className="h-3.5 w-3.5" aria-hidden="true" />} label="Time to a verdict"
              value={data.years_to_detect[0] ? `${data.years_to_detect[0].years} y` : "—"}
              sub={data.years_to_detect[0] ? `for a Sharpe of ${data.years_to_detect[0].sharpe}` : "no estimate"}
            />
          </div>

          <section className="space-y-2" aria-labelledby="algo-cards">
            <h2 id="algo-cards" className="text-sm font-semibold text-text-primary">Draft strategy cards</h2>
            <div className="grid gap-3 lg:grid-cols-3">
              {data.cards.map((c) => (
                <Card key={c.id} className="space-y-2 p-4" data-testid="algo-card">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <p className="text-sm font-medium text-text-primary">{c.name}</p>
                    <Badge variant="secondary" className="text-[10px]">{c.verdict_class}</Badge>
                  </div>
                  <p className="text-xs text-text-secondary">{c.summary}</p>
                  <p className="text-xs text-text-muted">Turnover: {c.turnover}</p>
                  <Disclosure title="Expectation and source" className="border-0 bg-surface-2">
                    <p className="text-xs text-text-secondary">Expected: {c.prior}</p>
                    <p className="text-xs text-text-muted">Quantpedia: {c.quantpedia}</p>
                  </Disclosure>
                </Card>
              ))}
            </div>
          </section>

          <section className="space-y-2" aria-labelledby="algo-cells">
            <h2 id="algo-cells" className="text-sm font-semibold text-text-primary">Paper cells</h2>
            <div className="grid gap-3 lg:grid-cols-2">
              {data.paper_cells.map((c) => (
                <Card key={c.id} className="space-y-2 p-4" data-testid="algo-paper-cell">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <p className="text-sm font-medium text-text-primary">{c.name}</p>
                    <Badge variant="outline" className="text-[10px]">{c.status}</Badge>
                  </div>
                  <p className="text-xs text-text-secondary">{c.rule}</p>
                  <Disclosure title="Instruments and evidence" className="border-0 bg-surface-2">
                    <p className="text-xs text-text-secondary">Instruments: {c.instruments}.</p>
                    <p className="text-xs text-text-secondary">Evidence: {c.evidence}</p>
                  </Disclosure>
                </Card>
              ))}
            </div>
          </section>

          <Card className="space-y-3 p-4">
            <div>
              <h2 className="text-sm font-semibold text-text-primary">How long a verdict takes</h2>
              <p className="text-xs text-text-secondary">Years of daily data to detect each Sharpe ratio (80 % power), full three-arm family.</p>
            </div>
            <ul className="space-y-2">
              {data.years_to_detect.map((r) => (
                <li key={r.sharpe} className="grid grid-cols-[5.5rem_1fr_13rem] items-center gap-3 text-xs">
                  <span className="text-text-secondary">Sharpe {r.sharpe}</span>
                  <span className="h-2 overflow-hidden rounded-full bg-surface-2">
                    <span className="block h-full rounded-full bg-accent" style={{ width: `${(r.years / maxYears) * 100}%` }} />
                  </span>
                  <span className="font-mono text-text-primary">about {r.years} years <span className="text-text-muted">({r.years_single_arm} one arm)</span></span>
                </li>
              ))}
            </ul>
          </Card>

          <Disclosure title="Looked at, not carded" summary="Quantpedia ideas that do not fit a small slow ETF sleeve">
            {data.not_carded.map((n) => (
              <p key={n.quantpedia} className="text-sm"><span className="font-medium">{n.quantpedia}</span>: {n.why}</p>
            ))}
          </Disclosure>

          <p className="text-xs text-text-muted">
            The intraday rules are measured on the <Link className="underline" to="/desk">derivatives desk</Link> as paper
            nulls; this page is the slow, cheap counterpart.
          </p>
        </>
      )}
    </div>
  );
}
