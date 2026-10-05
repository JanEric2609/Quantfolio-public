import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Card } from "../../components/ui/card";
import { api, type AdvisorCycleResponse } from "../../lib/api";
import { formatMetricNumber, formatMetricPercent } from "../../components/composed/MetricTile";
import { PaperRunCard } from "../portfolio/components/PaperRunCard";
import { formatCurrency, formatDate, formatDateTime, formatNumber, formatPercent } from "../../lib/format";

const fmt = (v: number | null | undefined, digits = 3) => formatMetricNumber(v, digits, "pending");
const pct = (v: number | null | undefined) => formatMetricPercent(v, 2, "pending");

export function AdvisorLoopTab() {
  const query = useQuery({
    queryKey: ["advisor-cycle-latest"],
    queryFn: () => api<AdvisorCycleResponse>("/api/advisor/cycle/latest"),
  });

  if (query.isLoading) {
    return <Card className="p-6 text-sm text-text-muted">Loading advisor loop…</Card>;
  }
  const data = query.data;
  if (!data || !data.portfolio_id) {
    return (
      <Card className="p-6 text-sm text-text-muted">
        No advisor cycle has run yet. The autonomous paper-trade cycle runs once per
        trading day; its first run seeds the paper portfolio from your real book.
      </Card>
    );
  }

  const decision = data.decision;
  const scorecard = data.scorecard;

  return (
    <div className="space-y-4">
      <p className="text-xs text-text-muted">
        The loop: <Link to="/discover" className="text-accent underline">Discover</Link> feeds
        ideas → this cycle trades them on paper →{" "}
        <Link to="/advisor/evolution" className="text-accent underline">Evolution</Link>{" "}
        pits champion vs challenger →{" "}
        <Link to="/graduation" className="text-accent underline">Graduation</Link> tests whether that paper
        record is statistically real. It is paper only: nothing here sets weights for your real book
        (only <Link to="/plan" className="text-accent underline">This month</Link> does), and the
        "What would change" tab is research.
      </p>
      <PaperRunCard portfolioId={data.portfolio_id} invalidate={[["advisor-cycle-latest"]]} />
      <Card className="p-6">
        <h3 className="text-sm font-semibold text-text-primary mb-2">Latest cycle</h3>
        {decision ? (
          <div className="space-y-3">
            <p className="text-xs text-text-muted">
              {formatDateTime(decision.review_date)} ·{" "}
              status {decision.status}
              {decision.optimizer_status ? ` · optimizer ${decision.optimizer_status}` : ""}
            </p>
            {decision.error && (
              <p className="text-xs text-danger">Error: {decision.error}</p>
            )}
            {(decision.decisions?.length ?? 0) > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-text-muted border-b border-border">
                      <th className="py-1 pr-3">Ticker</th>
                      <th className="py-1 pr-3">Action</th>
                      <th className="py-1 pr-3">Target wt</th>
                      <th className="py-1 pr-3">Confidence (raw → calibrated)</th>
                      <th className="py-1">Thesis</th>
                    </tr>
                  </thead>
                  <tbody>
                    {decision.decisions!.map((d) => (
                      <tr key={d.ticker + d.action} className="border-b border-border/50 align-top">
                        <td className="py-1.5 pr-3 font-medium">{d.ticker}</td>
                        <td className="py-1.5 pr-3 uppercase">{d.action}</td>
                        <td className="py-1.5 pr-3">{pct(d.target_weight)}</td>
                        <td className="py-1.5 pr-3 tabular-nums">
                          {fmt(d.confidence_raw ?? d.confidence, 2)}
                          {" → "}
                          {d.confidence_calibrated === null || d.confidence_calibrated === undefined
                            ? "uncalibrated"
                            : fmt(d.confidence_calibrated, 2)}
                        </td>
                        <td className="py-1.5 text-text-secondary">{d.thesis || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {(decision.funding_sells?.length ?? 0) > 0 && (
              <div>
                <h4 className="text-xs font-semibold text-text-primary mb-1">
                  Funding sells (optimiser, not the model)
                </h4>
                <ul className="text-xs text-text-secondary space-y-0.5">
                  {decision.funding_sells!.map((f, i) => (
                    <li key={i}>
                      <span className="font-medium">{f.ticker}</span> {formatCurrency(f.value, "EUR", { digits: 0 })} — {f.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {(decision.skipped?.length ?? 0) > 0 && (
              <div>
                <h4 className="text-xs font-semibold text-warn mb-1">Not executed</h4>
                <ul className="text-xs text-text-secondary space-y-0.5">
                  {decision.skipped!.map((s, i) => (
                    <li key={i}>
                      <span className="font-medium uppercase">{s.action}</span>{" "}
                      <span className="font-medium">{s.ticker}</span>: {s.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {decision.trade_flow && (
              <p className="text-xs text-text-muted">
                Trade budget — cash {formatCurrency(decision.trade_flow.cash_before, "EUR")} · traded{" "}
                {formatCurrency(decision.trade_flow.turnover_used, "EUR", { digits: 0 })} of{" "}
                {formatCurrency(decision.trade_flow.turnover_budget, "EUR", { digits: 0 })} cap (
                {formatPercent(decision.trade_flow.max_turnover_pct, { digits: 0 })} of NAV) · min ticket{" "}
                {formatCurrency(decision.trade_flow.min_ticket_eur, "EUR", { digits: 0 })} · fees{" "}
                {formatCurrency(decision.trade_flow.fees_paid, "EUR")}
              </p>
            )}
            {(decision.blocked?.length ?? 0) > 0 && (
              <div>
                <h4 className="text-xs font-semibold text-danger mb-1">Blocked by risk gate</h4>
                <ul className="text-xs text-text-secondary space-y-0.5">
                  {decision.blocked!.map((b, i) => (
                    <li key={i}>
                      <span className="font-medium">{b.symbol}</span>: {b.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {decision.risk_envelope && (
              <p className="text-xs text-text-muted">
                Risk envelope — VaR95 {pct(decision.risk_envelope.var_95_daily)} · CVaR95{" "}
                {pct(decision.risk_envelope.cvar_95_daily)} · MaxDD{" "}
                {pct(decision.risk_envelope.max_drawdown)}
              </p>
            )}
          </div>
        ) : (
          <p className="text-sm text-text-muted">No cycle decision recorded yet.</p>
        )}
      </Card>

      <Card className="p-6">
        <h3 className="text-sm font-semibold text-text-primary mb-2">Executed paper trades</h3>
        {data.trades.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-text-muted border-b border-border">
                  <th className="py-1 pr-3">Date</th>
                  <th className="py-1 pr-3">Ticker</th>
                  <th className="py-1 pr-3">Side</th>
                  <th className="py-1 pr-3">Qty</th>
                  <th className="py-1 pr-3">Price</th>
                  <th className="py-1 pr-3">Value</th>
                  <th className="py-1 pr-3">Fee</th>
                  <th className="py-1">Rationale</th>
                </tr>
              </thead>
              <tbody>
                {data.trades.map((t) => (
                  <tr key={t.id} className="border-b border-border/50 align-top">
                    <td className="py-1.5 pr-3 whitespace-nowrap">
                      {formatDate(t.executed_at)}
                    </td>
                    <td className="py-1.5 pr-3 font-medium">{t.ticker}</td>
                    <td className="py-1.5 pr-3 uppercase">{t.side}</td>
                    <td className="py-1.5 pr-3">{formatNumber(t.quantity, { digits: 4 })}</td>
                    <td className="py-1.5 pr-3">{formatNumber(t.price, { digits: 2 })}</td>
                    <td className="py-1.5 pr-3">{formatNumber(t.value, { digits: 2 })}</td>
                    <td className="py-1.5 pr-3 text-text-secondary">{t.fee ? formatNumber(t.fee, { digits: 2 }) : "—"}</td>
                    <td className="py-1.5 text-text-secondary">{t.rationale ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-text-muted">No trades executed yet.</p>
        )}
      </Card>

      <Card className="p-6">
        <h3 className="text-sm font-semibold text-text-primary mb-2">4-axis scorecard</h3>
        {scorecard ? (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <div>
              {/* Computed over this paper sleeve's own NAV returns (advisor/scorecard.py,
                  axes 1+4) — not the user's real DKB holdings (QuantLab) or a
                  prediction-accuracy metric, so label distinctly to avoid
                  confusion with the portfolio-level Sharpe shown elsewhere. */}
              <h4 className="text-xs text-text-muted mb-1">Risk-adjusted return (paper sleeve)</h4>
              <p>
                Sharpe (paper sleeve) {fmt(scorecard.risk_adjusted_return.sharpe)}
                {scorecard.risk_adjusted_return.sharpe_se != null && (
                  <span className="text-text-muted"> ± {fmt(scorecard.risk_adjusted_return.sharpe_se)}</span>
                )}
              </p>
              {scorecard.risk_adjusted_return.sharpe_ci_spans_zero === true && (
                <p className="text-xs text-warn">
                  Not distinguishable from zero at 95% — too few NAV returns
                  {scorecard.risk_adjusted_return.n_nav_returns != null &&
                    ` (${scorecard.risk_adjusted_return.n_nav_returns})`}
                  . Don't rank on it.
                </p>
              )}
              <p>Sortino (paper sleeve) {fmt(scorecard.risk_adjusted_return.sortino)}</p>
              <p>Calmar (paper sleeve) {fmt(scorecard.risk_adjusted_return.calmar)}</p>
            </div>
            <div>
              <h4 className="text-xs text-text-muted mb-1">Calibration</h4>
              <p>Brier {fmt(scorecard.calibration.brier_avg)}</p>
              <p>Log-loss {fmt(scorecard.calibration.log_loss_avg)}</p>
            </div>
            <div>
              <h4 className="text-xs text-text-muted mb-1">Magnitude accuracy</h4>
              <p>MZ slope {fmt(scorecard.magnitude_accuracy.mz_slope)}</p>
              <p>MZ R² {fmt(scorecard.magnitude_accuracy.mz_r2)}</p>
            </div>
            <div>
              <h4 className="text-xs text-text-muted mb-1">Downside discipline</h4>
              <p>Max DD {pct(scorecard.downside_discipline.max_drawdown)}</p>
              <p>CVaR95 {pct(scorecard.downside_discipline.cvar_95)}</p>
            </div>
            <p className="col-span-full text-xs text-text-muted">
              Window {scorecard.window_start} → {scorecard.window_end} ·{" "}
              {scorecard.n_resolved}/{scorecard.n_predictions} predictions matured
            </p>
          </div>
        ) : (
          <p className="text-sm text-text-muted">
            Scorecard pending — computes once predictions mature (21 trading days).
          </p>
        )}
      </Card>
    </div>
  );
}
