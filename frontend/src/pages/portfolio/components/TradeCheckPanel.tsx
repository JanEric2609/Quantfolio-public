import { useQuery } from "@tanstack/react-query";
import { api, type TradeCheckSummary } from "../../../lib/api";
import { formatCurrency, formatDate } from "../../../lib/format";

const eur = (v?: number | null, digits = 0) => formatCurrency(v, "EUR", { digits });

/**
 * The trade check in shadow (decision/trade_check.py): for each trade the
 * sleeve booked, did the edge its source has shown cover the cost? Nothing is
 * blocked yet; the panel shows which trades the check would have stopped and
 * the value the run would have with it on. Renders nothing for a run with no
 * checked trades (a manual book, or a run reset today).
 */
export function TradeCheckPanel({ portfolioId }: { portfolioId: string }) {
  const q = useQuery({
    queryKey: ["paper-trade-check", portfolioId],
    queryFn: () => api<TradeCheckSummary>(`/api/paper-portfolio/${portfolioId}/trade-check`),
  });
  const d = q.data;
  if (!d || typeof d.trades_checked !== "number" || d.trades_checked === 0 || !Array.isArray(d.recent)) return null;

  // What the blocked trades added against the index; turning the check on gives it up.
  const effect = -d.blocked_contribution_eur;
  return (
    <details className="rounded-md border border-border p-2">
      <summary className="cursor-pointer text-text-primary">
        Trade check (shadow): would have stopped {d.would_block} of {d.trades_checked} trades
        {d.checked_nav_eur != null && (
          <>
            {" · "}value with the check on <strong className="tabular-nums">{eur(d.checked_nav_eur)}</strong>
            {" ("}
            <span className={effect < 0 ? "text-danger" : "text-success"}>
              {effect >= 0 ? "+" : ""}
              {eur(effect)}
            </span>
            {")"}
          </>
        )}
      </summary>
      <div className="mt-2 space-y-2">
        <p className="text-text-muted">
          A trade passes when the edge its source has shown so far (calibrated calls, or the run's own resolved trades
          against MSCI World, shrunk toward a coin flip) is larger than its cost: fee, half the spread and tax on a
          realised gain (none while the NV certificate covers the year). Costs paid so far {eur(d.cost_paid_eur, 2)}, of
          which {eur(d.cost_of_blocked_eur, 2)} on trades the check would have stopped. {d.open} trade
          {d.open === 1 ? " is" : "s are"} still inside the 21 trading days they are measured over.
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-left tabular-nums">
            <thead className="text-text-muted">
              <tr>
                <th className="pr-2 font-normal">Day</th>
                <th className="pr-2 font-normal">Trade</th>
                <th className="pr-2 text-right font-normal">Cost</th>
                <th className="pr-2 text-right font-normal">Edge</th>
                <th className="pr-2 font-normal">Check</th>
                <th className="text-right font-normal">Added vs. index</th>
              </tr>
            </thead>
            <tbody>
              {d.recent.slice(0, 20).map((r) => (
                <tr key={r.trade_id} title={r.reason}>
                  <td className="pr-2">{formatDate(r.traded_on)}</td>
                  <td className="pr-2">
                    {r.side} {r.ticker} {eur(r.notional_eur)}
                  </td>
                  <td className="pr-2 text-right">{eur(r.cost_eur, 2)}</td>
                  <td className="pr-2 text-right">{eur(r.edge_eur, 2)}</td>
                  <td className={`pr-2 ${r.verdict === "block" ? "text-warn" : "text-success"}`}>
                    {r.verdict === "block" ? "would stop" : "pass"}
                  </td>
                  <td className="text-right">
                    {r.contribution_eur == null ? "–" : eur(r.contribution_eur, 2)}
                    {!r.resolved && r.contribution_eur != null ? " (open)" : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-text-muted">Shadow mode: the run trades as before. Estimates, not tax advice.</p>
      </div>
    </details>
  );
}
