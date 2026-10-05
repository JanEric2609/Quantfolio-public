import { formatDateTime, formatNumber, formatPercent } from "../lib/format";
import { useEffect, useState } from "react";
import {
  GraduationCap,
  CheckCircle2,
  XCircle,
  Lock,
  Sparkles,
  RefreshCw,
  AlertTriangle,
  ShieldCheck,
} from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Progress } from "../components/ui/progress";
import { toast } from "sonner";
import { Link } from "react-router-dom";
import {
  api,
  getGraduationStatus,
  generateGraduatedRecommendations,
  type GraduationStatus,
  type GraduationTransition,
  type GraduatedRecommendation,
} from "../lib/api";

const METRIC_LABELS: Record<string, string> = {
  dsr: "Deflated Sharpe",
  psr: "Probabilistic Sharpe",
  sharpe: "Sharpe (ann.)",
  sortino: "Sortino (ann.)",
  max_drawdown: "Max drawdown",
  min_track_record_length: "Min. track length (days)",
  observations: "Observations",
  n_trials: "Strategies (trials)",
  pbo: "Backtest overfitting (PBO)",
  competition_win_rate: "Competition win rate",
  paper_total_return: "Paper return",
  real_total_return: "Real return",
};

/**
 * Formats a metric value for display.
 *
 * @param key - The metric key; determines whether the value is treated as a percentage or formatted as a number
 * @param value - The metric value to format
 * @returns The formatted string, with null/undefined as "—", strings unchanged, percentage keys multiplied by 100 with one decimal place and "%", and other numbers as integers or two decimal places
 */
function fmtMetric(key: string, value: number | string | null): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (["dsr", "psr", "max_drawdown", "pbo", "competition_win_rate", "paper_total_return", "real_total_return"].includes(key)) {
    return formatPercent(value, { digits: 1 });
  }
  return Number.isInteger(value) ? String(value) : formatNumber(value, { digits: 2 });
}

/**
 * Displays LLM graduation status and track-record metrics, and enables real-portfolio recommendation generation.
 */
export function GraduationPage() {
  const [status, setStatus] = useState<GraduationStatus | null>(null);
  const [transitions, setTransitions] = useState<GraduationTransition[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);
  const [recs, setRecs] = useState<GraduatedRecommendation[] | null>(null);

  function load() {
    setLoading(true);
    setError(null);
    getGraduationStatus()
      .then(setStatus)
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load graduation status"))
      .finally(() => setLoading(false));
    api<GraduationTransition[]>("/api/graduation/transitions")
      .then(setTransitions)
      .catch(() => setTransitions([]));
  }

  useEffect(load, []);

  async function generate() {
    setGenerating(true);
    try {
      const res = await generateGraduatedRecommendations();
      setRecs(res.recommendations);
      toast.success(`Generated ${res.recommendations.length} recommendation(s).`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not generate recommendations");
    } finally {
      setGenerating(false);
    }
  }

  if (loading) {
    return <div className="p-6 text-text-secondary">Loading graduation status…</div>;
  }
  if (error || !status) {
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

  const pct = Math.round(status.overall_progress * 100);

  return (
    <div className="p-6 space-y-6 max-w-4xl">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <GraduationCap className="h-5 w-5" /> LLM Graduation
          </h1>
          <p className="text-sm text-text-secondary mt-1 max-w-2xl">
            The <strong>champion strategy</strong> only advises your{" "}
            <strong>real</strong> portfolio once it has statistically proven itself on
            the paper book — a Deflated Sharpe Ratio (deflated for every competing
            strategy), out-of-sample consistency, a drawdown ceiling, learning
            maturity, and a sustained 4-axis bar across rolling scorecards. If it
            later dips below the bar it <strong>de-graduates</strong> and
            recommendations pause. Advisory only, never auto-traded.
          </p>
        </div>
        <Badge variant={status.graduated ? "default" : "secondary"} className="shrink-0">
          {status.graduated ? "Graduated" : "In training"}
        </Badge>
      </div>

      {/* Passive core (ADR 0015 silent-period default) */}
      {!status.graduated && status.passive_core && (
        <Card className="border-accent/50">
          <CardContent className="pt-4 flex items-start gap-3">
            <ShieldCheck className="h-5 w-5 text-accent shrink-0 mt-0.5" />
            <div className="text-sm">
              <span className="font-medium">Passive core in effect: </span>
              {status.passive_core.message}{" "}
              <Link to="/evidence" className="text-accent underline">
                See evidence
              </Link>
              .
            </div>
          </CardContent>
        </Card>
      )}

      {/* Switch state */}
      {status.state && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Graduation switch</CardTitle>
            <CardDescription>
              {status.state.graduated
                ? `Graduated since ${formatDateTime(status.state.since)} — the `
                : "Off — the "}
              <Link to="/advisor/divergence" className="text-accent underline">
                what-would-change diff
              </Link>{" "}
              {status.state.graduated
                ? "is shown for research only: the LLM loop stays paper-only, and only the This month plan sets weights for your real book."
                : "renders as a read-only preview; no recommendations are surfaced."}
            </CardDescription>
          </CardHeader>
          {(status.state.last_transition_at || transitions.length > 0) && (
            <CardContent className="space-y-2">
              {status.state.last_reason && (
                <p className="text-xs text-text-secondary">
                  Last transition{" "}
                  {status.state.last_transition_at
                    ? formatDateTime(status.state.last_transition_at)
                    : "—"}
                  : {status.state.last_reason}
                </p>
              )}
              {transitions.length > 0 && (
                <div>
                  <div className="text-[11px] uppercase tracking-wide text-text-secondary mb-1">
                    History
                  </div>
                  <ul className="space-y-1">
                    {transitions.map((t) => (
                      <li key={t.id} className="text-xs flex items-start gap-2">
                        <Badge
                          variant={t.to_graduated ? "default" : "secondary"}
                          className="text-[10px] shrink-0"
                        >
                          {t.to_graduated ? "graduated" : "de-graduated"}
                        </Badge>
                        <span className="text-text-secondary">
                          {formatDateTime(t.created_at)} — {t.reason}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </CardContent>
          )}
        </Card>
      )}

      {/* Overall progress */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Overall progress</CardTitle>
          <CardDescription>{pct}% of graduation criteria met</CardDescription>
        </CardHeader>
        <CardContent>
          <Progress value={pct} />
        </CardContent>
      </Card>

      {/* Criteria */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Graduation criteria</CardTitle>
          <CardDescription>Every blocking criterion must pass.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {status.criteria.map((c) => (
            <div key={c.key} className="space-y-1">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  {c.passed ? (
                    <CheckCircle2 className="h-4 w-4 text-success" />
                  ) : (
                    <XCircle className="h-4 w-4 text-text-secondary" />
                  )}
                  <span className="text-sm font-medium">{c.label}</span>
                </div>
                <span className="text-xs text-text-secondary">{Math.round(c.progress * 100)}%</span>
              </div>
              <Progress value={Math.round(c.progress * 100)} />
              <p className="text-xs text-text-secondary">{c.detail}</p>
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Metrics */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Track-record metrics</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {Object.entries(METRIC_LABELS).map(([key, label]) => (
              <div key={key} className="rounded-md border border-border p-2">
                <div className="text-[11px] uppercase tracking-wide text-text-secondary">{label}</div>
                <div className="text-sm font-semibold">{fmtMetric(key, status.metrics[key] ?? null)}</div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Action */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Sparkles className="h-4 w-4" /> Real-portfolio recommendations
          </CardTitle>
          <CardDescription>
            {status.recommendations_unlocked
              ? "Unlocked. Recommendations appear on the Dossiers page for manual action."
              : "Locked until the LLM graduates."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button onClick={generate} disabled={!status.recommendations_unlocked || generating}>
            {status.recommendations_unlocked ? (
              <Sparkles className="h-4 w-4 mr-2" />
            ) : (
              <Lock className="h-4 w-4 mr-2" />
            )}
            {generating ? "Generating…" : "Generate recommendations"}
          </Button>

          {recs && recs.length > 0 && (
            <div className="mt-4 space-y-2">
              {recs.map((r, i) => (
                <div key={i} className="rounded-md border border-border p-3">
                  <div className="flex items-center gap-2">
                    <Badge variant="outline">{r.action}</Badge>
                    <span className="font-medium">{r.ticker}</span>
                    <span className="text-xs text-text-secondary">conf: {r.confidence}</span>
                  </div>
                  <p className="text-sm mt-1">{r.thesis}</p>
                </div>
              ))}
            </div>
          )}

          <p className="text-[11px] text-text-secondary mt-4">
            Estimate · not tax advice · not financial advice. Broker statements are the source of truth.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
