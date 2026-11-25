import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { PageHeader } from "../components/composed/PageHeader";
import { getAlgoOverview, type AlgoOverview } from "../lib/api";

export function AlgoPage() {
  const [data, setData] = useState<AlgoOverview | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    getAlgoOverview().then(setData).catch(() => setError(true));
  }, []);

  return (
    <div className="space-y-4">
      <PageHeader title="Algo trading (research)" subtitle="Slow, low-cost rules on paper. Nothing here is funded or ordered." />
      {error && <p role="alert" className="text-sm text-red-600">The overview could not be loaded.</p>}
      {data && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Status</CardTitle>
              <CardDescription>{data.note}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-1 text-sm">
              <p>Funded candidates: {data.funded_candidates}. Draft cards: {data.cards.length} of at most {data.max_arms} in family <code>{data.family}</code>.</p>
              <p>
                The intraday rules are measured on the <Link className="underline" to="/desk">derivatives desk</Link> as paper
                nulls; this section is the slow, cheap counterpart.
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Draft strategy cards</CardTitle>
              <CardDescription>Declared as risk overlays: judged on drawdown, not on beating buy-and-hold.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {data.cards.map((c) => (
                <div key={c.id} data-testid="algo-card">
                  <p className="font-medium">{c.name} <span className="text-muted-foreground">({c.quantpedia})</span></p>
                  <p>{c.summary}</p>
                  <p className="text-muted-foreground">Expected: {c.prior} Turnover: {c.turnover}.</p>
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Paper cells</CardTitle>
              <CardDescription>Pre-registered rules that must earn their place. Judged against costs and a placebo; nothing is funded.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {data.paper_cells.map((c) => (
                <div key={c.id} data-testid="algo-paper-cell">
                  <p className="font-medium">{c.name}</p>
                  <p>{c.rule}</p>
                  <p className="text-muted-foreground">Instruments: {c.instruments}. Evidence: {c.evidence} Status: {c.status}.</p>
                </div>
              ))}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>How long a verdict takes</CardTitle>
              <CardDescription>Years of daily data to detect a Sharpe ratio (one-sided, 80 % power), with the three-arm family's trials counted.</CardDescription>
            </CardHeader>
            <CardContent className="text-sm">
              <ul className="space-y-1">
                {data.years_to_detect.map((r) => (
                  <li key={r.sharpe}>Sharpe {r.sharpe}: about {r.years} years for the full three-arm family ({r.years_single_arm} for one arm)</li>
                ))}
              </ul>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Looked at, not carded</CardTitle>
              <CardDescription>Quantpedia ideas that do not fit a small slow ETF sleeve.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              {data.not_carded.map((n) => (
                <p key={n.quantpedia}><span className="font-medium">{n.quantpedia}</span>: {n.why}</p>
              ))}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
