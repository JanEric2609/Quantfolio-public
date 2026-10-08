import type { TrustAci, TrustTypeVerdict, TrustVerdict } from "../../lib/api";
import { Card } from "../../components/ui/card";
import { EvidenceChip } from "./EvidenceChip";
import { STATE_META, cadenceText, fmtDay, fmtNeeded, fmtInterval, fmtPct, fmtPp } from "./trustFormat";

function Stat({ label, value, sub }: { label: string; value: string; sub?: string | null }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-text-muted">{label}</dt>
      <dd className="font-mono text-sm font-semibold tabular-nums text-text-primary">{value}</dd>
      {sub ? <dd className="text-[11px] text-text-muted">{sub}</dd> : null}
    </div>
  );
}

/**
 * One prediction type: the evidence chip, then the numbers behind it. Every
 * number carries its N and an interval, or says why it has none yet.
 */
export function TypeSummary({
  type, verdict, headingLevel = 3,
}: { type: TrustTypeVerdict; verdict?: TrustVerdict; headingLevel?: 2 | 3 }) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const hitCi = fmtInterval(type.hit_ci, "pct");
  const excessCi = fmtInterval(type.mean_excess_ci, "pp");
  const coverage = type.range_coverage;
  const hasCalls = type.n > 0;
  return (
    <Card className="space-y-3 p-4" data-testid={`type-${type.type}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Heading className="text-sm font-semibold text-text-primary">{type.label}</Heading>
        <EvidenceChip type={type} />
      </div>
      <p className="text-xs text-text-secondary">{STATE_META[type.state].meaning}</p>

      {hasCalls ? (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
          <Stat
            label="Dates that beat the ETF"
            value={`${fmtPct(type.hit_rate)} (${type.hits} of ${type.n})`}
            sub={hitCi ? `90 % interval ${hitCi} (exact)` : null}
          />
          {type.benchmarked ? (
            <Stat
              label="Mean excess vs ETF"
              value={`${fmtPp(type.mean_excess)} per date`}
              sub={excessCi ? `90 % interval ${excessCi} (Newey-West)` : `interval needs ${verdict?.min_units_for_interval ?? "more"} dates`}
            />
          ) : (
            <Stat label="Compared with" value={type.benchmark_label} />
          )}
          <Stat
            label="Rebalance dates"
            value={`${type.n}${type.n_needed ? ` of ${fmtNeeded(type.n_needed, type.n_needed_is_lower_bound)}` : ""}`}
            sub={
              (type.n_needed ? `${cadenceText(type)}; ` : "") +
              `${type.n_calls ?? type.n} calls; ${type.call_hit_rate != null ? `${fmtPct(type.call_hit_rate)} of calls hit` : ""}` +
              (type.n_delisted ? `; ${type.n_delisted} stopped trading (kept, at the last close)` : "") +
              (type.n_unscored ? `; ${type.n_unscored} resolved calls could not be scored: no benchmark price` : "")
            }
          />
          <Stat
            label={type.benchmarked ? "Per-date evidence (secondary)" : "Evidence (e-value now)"}
            value={`skill ${type.e_skill.toFixed(1)} · harm ${type.e_harm.toFixed(1)}`}
            sub={
              !type.benchmarked
                ? "not tested: no benchmark to compare against"
                : `with calls in flight at their worst: skill ${(type.e_skill_lower ?? type.e_skill).toFixed(1)} · harm ${(type.e_harm_lower ?? type.e_harm).toFixed(1)}` +
                  (type.family ? `; the state comes from the daily test (${type.family})` : "") +
                  (verdict?.ebh_threshold != null ? `, which needs ${verdict.ebh_threshold}` : "")
            }
          />
        </dl>
      ) : (
        <p className="text-sm text-text-secondary">
          No resolved calls yet{type.next_resolution_at ? `; the first resolves ${fmtDay(type.next_resolution_at)}` : ""}.
        </p>
      )}

      {type.calibration_caption ? <p className="text-sm text-text-primary">{type.calibration_caption}</p> : null}
      {type.brier != null ? (
        <p className="text-xs text-text-muted">
          Brier {type.brier.toFixed(3)} on {type.n_stated_p} calls with a stated probability
          {type.bss != null
            ? `; skill vs the base rate ${type.bss.toFixed(2)}${
                type.bss_ci ? ` (90 % interval ${type.bss_ci[0].toFixed(2)} to ${type.bss_ci[1].toFixed(2)})` : ` (interval needs ${verdict?.min_calls_for_interval ?? "more"} calls)`
              }`
            : ""}
          .
        </p>
      ) : null}
      {coverage && coverage.n > 0 ? (
        <p className="text-xs text-text-muted">
          Stated ranges ({fmtPct(coverage.nominal)} nominal): the outcome landed inside {coverage.k} of {coverage.n} times
          {coverage.ci_low != null && coverage.ci_high != null
            ? ` (90 % interval ${fmtPct(coverage.ci_low)} to ${fmtPct(coverage.ci_high)})`
            : ""}
          .
        </p>
      ) : null}
      {coverage?.aci && coverage.n > 0 ? <AciLine aci={coverage.aci} /> : null}
      {hasCalls && type.next_resolution_at ? (
        <p className="text-xs text-text-muted">Next resolution {fmtDay(type.next_resolution_at)}.</p>
      ) : null}
      {type.note ? <p className="text-xs text-text-muted">{type.note}</p> : null}
    </Card>
  );
}

/** ADR 0018 §6: the online conformal correction of the stated ranges. */
function AciLine({ aci }: { aci: TrustAci }) {
  if (!aci.active) {
    return (
      <p className="text-xs text-text-muted" data-testid="aci-waiting">
        Range correction (adaptive conformal) starts after {aci.weeks_needed} matured weeks; {aci.matured_weeks} so far.
      </p>
    );
  }
  const widen = aci.widen_now;
  const how =
    widen == null
      ? "cannot be sized yet"
      : widen >= 0
        ? `would widen each range by ${fmtPct(widen, 0)} of its width on each side`
        : `would narrow each range by ${fmtPct(-widen, 0)} of its width on each side`;
  return (
    <p className="text-xs text-text-muted" data-testid="aci-active">
      Range correction (adaptive conformal): to hold {fmtPct(1 - aci.alpha_target)}, the next range {how}.
      {aci.corrected && aci.raw_same_dates
        ? ` Since it started: corrected ranges held ${aci.corrected.k} of ${aci.corrected.n}, the stated ones ${aci.raw_same_dates.k}.`
        : ""}
    </p>
  );
}
