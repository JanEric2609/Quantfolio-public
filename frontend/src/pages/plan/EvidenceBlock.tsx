import { Link } from "react-router-dom";
import { Badge } from "../../components/ui/badge";
import { formatCurrency, formatDate } from "../../lib/format";
import type { MonthlyPlan, PlanEtfSuggestions } from "../../lib/api";
import { pct } from "./shared";

/** Bond, cash and gold lines from the owner's targets (rule-based, not a forecast). */
export function EtfLines({ suggestions }: { suggestions: PlanEtfSuggestions }) {
  return (
    <div className="space-y-4">
      {suggestions.lines.map((line) => (
        <div key={line.line} data-testid={`etf-line-${line.line}`} className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium text-text-primary">{line.label}</span>
            <Badge variant="secondary">Rule, not a forecast</Badge>
            <span className="ml-auto text-xs tabular-nums text-text-secondary">
              now {pct(line.current_pct)} · target {pct(line.target_pct)}
            </span>
          </div>
          {line.status === "under" && line.fund ? (
            <p className="text-sm text-text-secondary">
              <span className="font-medium text-text-primary">
                Add about {formatCurrency(line.amount_eur ?? 0, "EUR")}: {line.fund.isin}.{" "}
              </span>
              {line.fund.why}
            </p>
          ) : (
            <p className="text-sm text-text-secondary">Within the band; nothing to do.</p>
          )}
          {line.warning && <p className="text-sm text-warn">{line.warning}</p>}
          {line.evidence && <p className="text-sm text-text-secondary">{line.evidence}</p>}
          <p className="text-xs text-text-muted">Tax (estimate, not tax advice): {line.tax_note}</p>
        </div>
      ))}
    </div>
  );
}

/** What the capital ladder allows for stock picks, the lab gate's last run, and the factor-tilt check. */
export function EvidenceBlock({ evidence }: { evidence: NonNullable<MonthlyPlan["evidence"]> }) {
  const gate = evidence.satellite.gate;
  const ladder = evidence.satellite.ladder;
  const picks = evidence.satellite.picks ?? [];
  return (
    <div className="space-y-2 text-sm text-text-secondary">
      <p>
        <span className="font-medium text-text-primary">Stock picks:</span>{" "}
        {ladder ? (
          <>
            {ladder.weight_pct.toFixed(1)} % of the book, funded by the{" "}
            {ladder.slot === "llm" ? "LLM advisor" : "rules control (Mandate B)"} at stage {ladder.stage} (
            {ladder.stage_name}) on the <Link to="/graduation" className="text-accent underline-offset-2 hover:underline">capital ladder</Link>.
          </>
        ) : (
          <>
            0 %: no candidate is past the Paper stage of the{" "}
            <Link to="/graduation" className="text-accent underline-offset-2 hover:underline">capital ladder</Link>.
          </>
        )}
      </p>
      {picks.length > 0 && (
        <ul className="ml-4 list-disc text-xs">
          {picks.map((p) => (
            <li key={p.isin ?? p.ticker ?? p.name}>
              {p.name}
              {p.ticker ? ` (${p.ticker})` : ""}: {p.weight_pct.toFixed(2)} % of the book
            </li>
          ))}
        </ul>
      )}
      <p className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-text-primary">Lab evidence gate:</span>
        {gate ? (
          <>
            checked {gate.as_of ? formatDate(gate.as_of) : "on an unknown date"} against {gate.n_trials} trials
            {gate.stale && <Badge variant="warning">out of date</Badge>}
          </>
        ) : (
          "not checked yet"
        )}
      </p>
      <p>
        <span className="font-medium text-text-primary">Factor tilt:</span>{" "}
        {evidence.tilt.unlocked
          ? "evidence passed and a tilt fund is chosen."
          : evidence.tilt.passed
            ? "evidence passed, but no tilt fund is chosen. Set one in the plan settings to use it."
            : "no factor strategy has passed its evidence check yet."}
      </p>
      <Link to="/evidence" className="text-accent underline-offset-2 hover:underline">Open the evidence page</Link>
    </div>
  );
}

