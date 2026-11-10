import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Badge } from "../ui/badge";
import { LineChart } from "../charts/LineChart";
import { formatDate, formatNumber, formatPercent } from "../../lib/format";
import type { LadderCandidateState, LadderState } from "../../lib/api";

const SLOT_LABELS: Record<string, string> = {
  llm: "LLM advisor (champion lineage)",
  rules: "Rules control (Mandate B)",
};

const STAGES = ["Paper", "Tuition", "Earned", "Proven"];

function pct(v: number | null | undefined, digits = 1): string {
  return v == null || !Number.isFinite(v) ? "—" : formatPercent(v, { digits });
}

function CandidateCard({ c }: { c: LadderCandidateState }) {
  const days = c.history.map((h) => h.day);
  const series = [
    { name: "Skill e-value", data: c.history.map((h, i) => [days[i], h.e_skill ?? NaN] as [string, number]) },
    { name: "Harm e-value", data: c.history.map((h, i) => [days[i], h.e_harm ?? NaN] as [string, number]) },
    { name: `Earned (${c.thresholds.earned})`, data: days.map((d) => [d, c.thresholds.earned] as [string, number]) },
    { name: `Proven (${c.thresholds.proven})`, data: days.map((d) => [d, c.thresholds.proven] as [string, number]) },
  ];
  const ddShare = c.drawdown_band ? c.relative_drawdown / c.drawdown_band : null;
  return (
    <Card data-testid={`ladder-${c.slot}`}>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">{SLOT_LABELS[c.slot] ?? c.slot}</CardTitle>
            <CardDescription>
              Epoch {c.epoch} since {formatDate(c.born_at)} · {c.days_of_evidence} trading days of paired returns
            </CardDescription>
          </div>
          <Badge variant={c.stage >= 2 ? "default" : "secondary"}>
            Stage {c.stage}: {c.stage_name} · {formatNumber(c.weight_pct, { digits: 1 })} %
          </Badge>
        </div>
        <ol className="mt-2 flex flex-wrap gap-1 text-xs" aria-label="Ladder stages">
          {STAGES.map((name, i) => (
            <li
              key={name}
              className={`rounded px-2 py-0.5 ${i === c.stage ? "bg-accent text-white" : "bg-surface-2 text-text-muted"}`}
            >
              {i} {name}
            </li>
          ))}
        </ol>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p>{c.next_step}</p>
        <dl className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <div>
            <dt className="text-xs text-text-muted">Skill e-value</dt>
            <dd className="tabular-nums">{formatNumber(c.e_skill, { digits: 2 })}</dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Harm e-value</dt>
            <dd className="tabular-nums">{formatNumber(c.e_harm, { digits: 2 })} / {c.thresholds.harm}</dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Tracking error</dt>
            <dd className="tabular-nums">{pct(c.tracking_error)}</dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Relative drawdown / band</dt>
            <dd className="tabular-nums">
              {pct(c.relative_drawdown)} / {pct(c.drawdown_band)}
              {ddShare != null && ddShare > 0.8 && <span className="text-danger"> (near the band)</span>}
            </dd>
          </div>
        </dl>
        {c.history.length > 1 && (
          <LineChart
            ariaLabel={`Evidence of the ${c.slot} candidate against the thresholds`}
            className="h-56"
            xType="category"
            yLog
            series={series.map((s) => ({ ...s, data: s.data.filter((p) => Number.isFinite(p[1]) && p[1] > 0) }))}
            yFormat={(v) => formatNumber(v, { digits: 2 })}
          />
        )}
        {c.bayes && (
          <p className="text-xs text-text-muted">
            Bayesian view (display only): prior alpha {pct(c.bayes.prior_mean)} ± {pct(c.bayes.prior_sd)} a year;
            posterior {pct(c.bayes.posterior_mean)} ± {pct(c.bayes.posterior_sd)}; a quarter-Kelly weight of{" "}
            {pct(c.bayes.quarter_kelly_weight)}. {c.bayes.note}
          </p>
        )}
        {c.confidence_sequence && (
          <p className="text-xs text-text-muted">
            Alpha lies between {pct(c.confidence_sequence.alpha_low)} and {pct(c.confidence_sequence.alpha_high)} a
            year ({pct(c.confidence_sequence.level, 0)} confidence sequence, valid at any stopping time).
          </p>
        )}
        <p className="text-xs text-text-muted">Authorisation expires {formatDate(c.expires_at)}; renewal starts a new epoch.</p>
      </CardContent>
    </Card>
  );
}

export function LadderCards({ state }: { state: LadderState }) {
  const g = state.epoch_guarantee;
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Capital ladder</CardTitle>
          <CardDescription>
            How much of your book the satellite (single stocks) may hold, given the evidence. It caps what an
            unproven idea can cost; it does not detect skill, and at realistic edges it stays at Paper or Tuition
            for years.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <p>
            {state.funding ? (
              <>
                This month the satellite target is{" "}
                <strong>{formatNumber(state.funding.weight_pct, { digits: 1 })} %</strong>, funded by the{" "}
                {SLOT_LABELS[state.funding.slot] ?? state.funding.slot} at stage {state.funding.stage} (
                {state.funding.stage_name}), as of {formatDate(state.funding.as_of)}.
              </>
            ) : (
              <>This month the satellite target is <strong>0 %</strong>: no candidate is past the Paper stage.</>
            )}{" "}
            Stage changes take effect at the next monthly plan.
          </p>
          <p className="text-xs text-text-muted">
            A candidate with no skill reaches Earned in its first epoch with a chance of at most{" "}
            {pct(g.earned_epoch1 / 2, 0)}, Proven at most {pct(g.proven_epoch1 / 2, 1)}; over every epoch of both slots
            together at most {pct(g.earned_ever_max, 0)} and {pct(g.proven_ever_max, 0)}.
          </p>
        </CardContent>
      </Card>
      {state.candidates.length === 0 ? (
        <Card className="p-5 text-sm text-text-muted">No candidate is registered yet; the nightly ladder job registers them.</Card>
      ) : (
        state.candidates.map((c) => <CandidateCard key={c.candidate_id} c={c} />)
      )}
    </div>
  );
}
