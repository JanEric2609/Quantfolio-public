import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation } from "@tanstack/react-query";
import { api, type Recommendation, type RecommendationGenerateResult } from "../../lib/api";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Label } from "../../components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";

const RECOMMENDATION_MODES = ["long_term", "short_term"] as const;
const UNIVERSES = ["default_global", "etfs", "stocks", "portfolio", "watchlist"] as const;

export function GenerateTab() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<string>("long_term");
  const [universe, setUniverse] = useState<string>("default_global");
  const [limit, setLimit] = useState(5);
  const [result, setResult] = useState<RecommendationGenerateResult | null>(null);

  const generate = useMutation({
    mutationFn: () =>
      api<RecommendationGenerateResult>("/api/ai/recommendations/generate", {
        method: "POST",
        body: JSON.stringify({ mode, universe, limit }),
      }),
    onSuccess: (data) => setResult(data),
  });

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader><CardTitle>Generate Dossiers</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <div className="space-y-2">
              <Label>Mode</Label>
              <select className="w-full h-10 rounded-md border border-border bg-surface-2 px-3 text-sm" value={mode} onChange={e => setMode(e.target.value)}>
                {RECOMMENDATION_MODES.map(m => <option key={m} value={m}>{m.replace("_", " ")}</option>)}
              </select>
            </div>
            <div className="space-y-2">
              <Label>Universe</Label>
              <select className="w-full h-10 rounded-md border border-border bg-surface-2 px-3 text-sm" value={universe} onChange={e => setUniverse(e.target.value)}>
                {UNIVERSES.map(u => <option key={u} value={u}>{u.replace("_", " ")}</option>)}
              </select>
            </div>
            <div className="space-y-2">
              <Label>Limit</Label>
              <Input type="number" min={1} max={20} value={limit} onChange={e => setLimit(Number(e.target.value))} />
            </div>
          </div>
          <Button onClick={() => generate.mutate()} disabled={generate.isPending}>
            {generate.isPending ? "Generating..." : "Generate"}
          </Button>
        </CardContent>
      </Card>

      {result && (
        <Card>
          <CardHeader><CardTitle>Results</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            {result.created?.length > 0 && (
              <div>
                <p className="text-sm text-text-secondary mb-2">Created recommendations:</p>
                <ul className="space-y-2">
                  {result.created.map((rec: Recommendation) => (
                    <li key={rec.id}>
                      <button
                        className="text-sm text-accent hover:underline"
                        onClick={() => rec.ticker && navigate(`/research/stocks/${encodeURIComponent(rec.ticker)}`)}
                      >
                        {rec.ticker} — {rec.verdict}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {result.failed?.length > 0 && (
              <div>
                <p className="text-sm text-danger mb-2">Failed:</p>
                <ul className="space-y-1">
                  {result.failed.map((f: { ticker: string; message: string }, i: number) => (
                    <li key={i} className="text-sm text-text-secondary">{f.ticker}: {f.message}</li>
                  ))}
                </ul>
              </div>
            )}
            {result.message && <p className="text-sm text-text-muted">{result.message}</p>}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
