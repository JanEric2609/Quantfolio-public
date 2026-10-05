import { Card } from "../../../../components/ui/card";
import type { RiskAssessment } from "../../../../lib/api";

const MIN_RETURNS_FOR_RATIOS = 30;

function pct(value: number, digits = 2) {
  return `${(value * 100).toFixed(digits)} %`;
}

function Tile({ label, value, hint, tone }: { label: string; value: string; hint: string; tone?: "bad" | "warn" | "muted" }) {
  const toneClass = tone === "bad" ? "text-danger" : tone === "warn" ? "text-warn" : tone === "muted" ? "text-text-muted" : "text-text-primary";
  return (
    <Card className="space-y-1 p-4">
      <p className="text-xs uppercase tracking-wide text-text-secondary">{label}</p>
      <p className={`font-mono text-2xl font-semibold tabular-nums ${toneClass}`}>{value}</p>
      <p className="text-[11px] text-text-muted">{hint}</p>
    </Card>
  );
}

/** What the return-based numbers rest on, so a figure is never read without its sample. */
export function ReturnBasisNote({ risk }: { risk: RiskAssessment }) {
  const b = risk.return_basis;
  if (b.n_obs === 0) {
    return (
      <p role="status" className="text-sm text-text-secondary">
        {b.message ?? "No price history for your holdings yet."} VaR, drawdown and the risk ratios need daily prices.
        {b.missing_history.length > 0 ? ` Missing: ${b.missing_history.join(", ")}.` : ""}
      </p>
    );
  }
  return (
    <p className="text-xs text-text-secondary">
      Based on {b.n_obs} daily returns of your current holdings ({b.start} to {b.end}), weighted by position value. This is
      price history, not account balances: deposits and purchases do not count as returns.
      {b.missing_history.length > 0 ? ` Left out for lack of price history: ${b.missing_history.join(", ")}.` : ""}
    </p>
  );
}

/** VaR, drawdown and the risk-adjusted ratios; the ratios wait for 30 daily returns. */
export function RiskMetrics({ risk }: { risk: RiskAssessment }) {
  const n = risk.return_basis.n_obs;
  const hasReturns = n > 0;
  const ratiosReady = !risk.insufficient_history;
  const ratioHint = ratiosReady ? "annualised, over the same window" : `${n} of ${MIN_RETURNS_FOR_RATIOS} daily returns needed`;
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <Tile
        label="VaR 95 % (1 day)"
        value={hasReturns ? pct(risk.var_95) : "—"}
        hint="loss exceeded on about 1 day in 20"
        tone={hasReturns && risk.var_95 > 0.03 ? "warn" : undefined}
      />
      <Tile label="VaR 99 % (1 day)" value={hasReturns ? pct(risk.var_99) : "—"} hint="loss exceeded on about 1 day in 100" />
      <Tile
        label="Max drawdown"
        value={hasReturns ? pct(risk.max_drawdown, 1) : "—"}
        hint="worst peak to trough in the window"
        tone={hasReturns && risk.max_drawdown < -0.2 ? "bad" : undefined}
      />
      <Tile
        label="Current drawdown"
        value={hasReturns ? pct(risk.current_drawdown, 1) : "—"}
        hint="distance from the window's peak"
        tone={hasReturns && risk.current_drawdown < -0.1 ? "bad" : undefined}
      />
      <Tile label="Sharpe" value={ratiosReady ? risk.sharpe_ratio.toFixed(2) : "Too early"} hint={ratioHint} tone={ratiosReady ? undefined : "muted"} />
      <Tile label="Sortino" value={ratiosReady ? risk.sortino_ratio.toFixed(2) : "Too early"} hint={ratioHint} tone={ratiosReady ? undefined : "muted"} />
    </div>
  );
}
