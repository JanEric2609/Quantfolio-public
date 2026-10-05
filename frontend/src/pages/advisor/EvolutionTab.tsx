import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Swords, Trophy, BookOpenText, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { Card } from "../../components/ui/card";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { api, type EvolutionStatus, type EvolutionStrategy } from "../../lib/api";
import { formatDate, formatNumber } from "../../lib/format";
import { formatMetricNumber, formatMetricPercent } from "../../components/composed/MetricTile";

const fmt = (v: number | null | undefined, digits = 3) => formatMetricNumber(v, digits, "pending");
const pct = (v: number | null | undefined) => formatMetricPercent(v, 2, "pending");

/** "insufficient data" reads as broken; when we know the next batch of
 * predictions' resolve date, surface an ETA instead. */
const compositeLabel = (nextResolutionAt: string | null | undefined) =>
  nextResolutionAt ? `next evaluation ~${formatDate(nextResolutionAt)}` : "insufficient data";

function StrategyCard({ label, strategy }: { label: string; strategy: EvolutionStrategy | null }) {
  if (!strategy) {
    return (
      <Card className="p-5">
        <h3 className="text-sm font-semibold mb-1">{label}</h3>
        <p className="text-sm text-text-muted">
          Not created yet — the first evolution round spawns it.
        </p>
      </Card>
    );
  }
  const sc = strategy.scorecard;
  const framing = strategy.config["prompt_framing"] as string | undefined;
  return (
    <Card className="p-5 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold flex items-center gap-2">
          {label === "Champion" ? <Trophy className="h-4 w-4 text-accent" /> : <Swords className="h-4 w-4" />}
          {label}
        </h3>
        <Badge variant={label === "Champion" ? "default" : "secondary"}>
          composite{" "}
          {strategy.composite === null
            ? compositeLabel(strategy.next_resolution_at)
            : formatNumber(strategy.composite, { digits: 3 })}
        </Badge>
      </div>
      {framing && <p className="text-xs italic text-text-secondary">“{framing}”</p>}
      {sc ? (
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          {/* Computed over this sleeve's own paper NAV returns (advisor/scorecard.py,
              axes 1+4) — not the user's real DKB holdings shown in QuantLab, and not
              a prediction-accuracy metric — labelled distinctly to avoid confusion. */}
          <span className="text-text-muted">Sharpe (paper sleeve)</span>
          <span className="tabular-nums">{fmt(sc.risk_adjusted_return.sharpe)}</span>
          <span className="text-text-muted">Brier (calibration)</span>
          <span className="tabular-nums">{fmt(sc.calibration.brier_avg)}</span>
          <span className="text-text-muted">MZ slope / R²</span>
          <span className="tabular-nums">
            {fmt(sc.magnitude_accuracy.mz_slope, 2)} / {fmt(sc.magnitude_accuracy.mz_r2, 2)}
          </span>
          <span className="text-text-muted">Max drawdown</span>
          <span className="tabular-nums">{pct(sc.downside_discipline.max_drawdown)}</span>
          <span className="text-text-muted">Matured predictions</span>
          <span className="tabular-nums">{sc.n_resolved}/{sc.n_predictions}</span>
        </div>
      ) : (
        <p className="text-xs text-text-muted">Scorecard pending — no matured predictions yet.</p>
      )}
      <p className="text-[11px] text-text-muted">
        {strategy.promoted_at
          ? `Champion since ${formatDate(strategy.promoted_at)}`
          : `Spawned ${formatDate(strategy.created_at)}`}
      </p>
    </Card>
  );
}

export function EvolutionTab() {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["advisor-evolution"],
    queryFn: () => api<EvolutionStatus>("/api/advisor/evolution"),
  });
  const runRound = useMutation({
    mutationFn: () => api<{ verdict: string }>("/api/advisor/evolution/run", { method: "POST" }),
    onSuccess: (res) => {
      toast.success(`Evolution round complete: ${res.verdict.replaceAll("_", " ")}`);
      queryClient.invalidateQueries({ queryKey: ["advisor-evolution"] });
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Round failed"),
  });

  if (query.isLoading) {
    return <Card className="p-6 text-sm text-text-muted">Loading evolution arena…</Card>;
  }
  const data = query.data;
  if (!data) {
    return <Card className="p-6 text-sm text-text-muted">Evolution status unavailable.</Card>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <p className="text-sm text-text-secondary max-w-2xl">
          The champion strategy defends its 4-axis score against a mutated challenger every
          trading day. A challenger that wins decisively over enough decisions is promoted;
          the champion's sustained record feeds the{" "}
          <Link to="/graduation" className="text-accent underline">graduation check</Link>.
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => runRound.mutate()}
          disabled={runRound.isPending}
        >
          <RefreshCw className="h-4 w-4 mr-2" />
          {runRound.isPending ? "Running…" : "Run round now"}
        </Button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <StrategyCard label="Champion" strategy={data.champion} />
        <StrategyCard label="Challenger" strategy={data.challenger} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card className="p-5">
          <h3 className="text-sm font-semibold mb-3 flex items-center gap-2">
            <Swords className="h-4 w-4" /> Recent rounds
          </h3>
          {data.rounds.length === 0 ? (
            <p className="text-sm text-text-muted">
              No rounds recorded yet — the arena runs each trading day at 10:30 UTC.
            </p>
          ) : (
            <div className="space-y-3">
              {data.rounds.map((run) => (
                <div key={run.run_id} className="rounded-md border border-border p-3 text-xs space-y-1">
                  <div className="flex items-center justify-between">
                    <span className="text-text-muted">
                      {formatDate(run.started_at)} · {run.status}
                    </span>
                  </div>
                  {run.decisions.map((d) => (
                    <div key={d.portfolio_id + d.round} className="flex items-center justify-between">
                      <span className="truncate">
                        round {d.round} ·{" "}
                        {d.portfolio_id === data.champion?.portfolio_id ? "champion" : "challenger"}
                      </span>
                      <span className="tabular-nums flex items-center gap-2">
                        {d.score.composite === null || d.score.composite === undefined
                          ? compositeLabel(
                              d.portfolio_id === data.champion?.portfolio_id
                                ? data.champion?.next_resolution_at
                                : data.challenger?.next_resolution_at,
                            )
                          : formatNumber(d.score.composite, { digits: 3 })}
                        {d.winner && <Badge className="text-[10px]">winner</Badge>}
                      </span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
          {data.promotion_history.length > 0 && (
            <div className="mt-4">
              <h4 className="text-xs font-semibold text-text-secondary mb-1">Promotion history</h4>
              <ul className="text-xs text-text-secondary space-y-0.5">
                {data.promotion_history.map((p) => (
                  <li key={p.strategy_id}>
                    {formatDate(p.promoted_at)} — strategy{" "}
                    <span className="font-mono">{p.strategy_id.slice(0, 8)}</span> became champion
                    {p.retired_at ? ` (retired ${formatDate(p.retired_at)})` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>

        <Card className="p-5">
          <h3 className="text-sm font-semibold mb-3 flex items-center gap-2">
            <BookOpenText className="h-4 w-4" /> Reflection lessons
          </h3>
          {data.lessons.length === 0 ? (
            <p className="text-sm text-text-muted">
              No lessons yet — after each scored cycle the LLM distils what worked and what
              hurt into lessons it reads before the next trade.
            </p>
          ) : (
            <ul className="space-y-2">
              {data.lessons.map((lesson) => (
                <li key={lesson.id} className="rounded-md border border-border p-2.5 text-xs">
                  <p className={lesson.active ? "" : "opacity-50 line-through"}>{lesson.text}</p>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {(lesson.tags.signals ?? []).map((s) => (
                      <Badge key={s} variant="secondary" className="text-[10px]">{s}</Badge>
                    ))}
                    {lesson.tags.regime && (
                      <Badge variant="outline" className="text-[10px]">{lesson.tags.regime}</Badge>
                    )}
                    {(lesson.tags.sectors ?? []).map((s) => (
                      <Badge key={s} variant="outline" className="text-[10px]">{s}</Badge>
                    ))}
                    <span className="ml-auto text-text-muted tabular-nums">
                      rank {formatNumber(lesson.rank, { digits: 2 })}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
