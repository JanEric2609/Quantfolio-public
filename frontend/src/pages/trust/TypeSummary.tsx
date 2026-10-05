import type { TrustTypeVerdict } from "../../lib/api";
import { Card } from "../../components/ui/card";
import { EvidenceChip } from "./EvidenceChip";
import { STATE_META, aboutHundreds, fmtDay, fmtInterval, fmtPct, fmtPp } from "./trustFormat";

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
export function TypeSummary({ type, headingLevel = 3 }: { type: TrustTypeVerdict; headingLevel?: 2 | 3 }) {
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
              sub={excessCi ? `90 % interval ${excessCi} (Newey-West)` : "interval needs 8 dates"}
            />
          ) : (
            <Stat label="Compared with" value={type.benchmark_label} />
          )}
          <Stat
            label="Rebalance dates"
            value={`${type.n}${type.n_needed ? ` of ~${aboutHundreds(type.n_needed)}` : ""}`}
            sub={
              `${type.n_calls ?? type.n} calls; ${type.call_hit_rate != null ? `${fmtPct(type.call_hit_rate)} of calls hit` : ""}` +
              (type.n_delisted ? `; ${type.n_delisted} stopped trading (kept, at the last close)` : "")
            }
          />
          <Stat
            label="Evidence (e-value now)"
            value={`skill ${type.e_skill.toFixed(1)} · harm ${type.e_harm.toFixed(1)}`}
            sub={
              type.n_tests
                ? `e-BH over ${type.n_tests} looks: the strongest needs ${Math.ceil(type.n_tests / 0.05)}`
                : "20 or more is evidence"
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
                type.bss_ci ? ` (90 % interval ${type.bss_ci[0].toFixed(2)} to ${type.bss_ci[1].toFixed(2)})` : " (interval needs 15 calls)"
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
      {hasCalls && type.next_resolution_at ? (
        <p className="text-xs text-text-muted">Next resolution {fmtDay(type.next_resolution_at)}.</p>
      ) : null}
      {type.note ? <p className="text-xs text-text-muted">{type.note}</p> : null}
    </Card>
  );
}
