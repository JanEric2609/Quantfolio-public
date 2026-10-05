import { formatCurrency, formatPercent } from "../../../lib/format";
import type { RealRebalanceResponse } from "../../../lib/api";

const pct = (v?: number | null) => formatPercent(v, { digits: 1 });
const eur = (v?: number | null) => (v == null ? "—" : formatCurrency(v, "EUR"));

/**
 * Band rebalancing toward the chosen target: this month's contribution goes to
 * the most underweight lines, and a line is sold only when it is more than the
 * band above target and a year of contributions would not fix it. A sale shows
 * the gain it realises and the flat-rate tax on it before any allowance.
 */
export function RebalanceCard({
  data, loading, target, onTarget,
}: {
  data?: RealRebalanceResponse;
  loading: boolean;
  target: string;
  onTarget: (target: string) => void;
}) {
  const methods = data?.methods ?? {
    equal: "Equal weight (1/N)", inverse_vol: "Inverse volatility", min_variance: "Minimum variance",
    erc: "Equal risk contribution", hrp: "Hierarchical risk parity",
  };
  const lines = data?.lines ?? [];
  return (
    <section className="rounded-md p-3 bg-surface" aria-label="Rebalancing" data-testid="rebalance-card">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-text-primary">Rebalancing</h2>
        <label className="flex items-center gap-1 text-xs">
          <span className="text-text-muted">Target</span>
          <select
            className="rounded border border-line bg-surface px-2 py-1"
            value={target}
            onChange={(e) => onTarget(e.target.value)}
            aria-label="Target allocation"
          >
            {Object.entries(methods).map(([k, label]) => (
              <option key={k} value={k}>{label}</option>
            ))}
          </select>
        </label>
      </div>
      {loading ? (
        <p className="text-xs text-text-muted">Computing…</p>
      ) : !data?.available ? (
        <p className="text-xs text-text-secondary">{data?.diagnostics?.reason ?? "Not enough price history to rebalance."}</p>
      ) : (
        <>
          <p className="mb-2 text-xs text-text-secondary">
            {eur(data.contribution_eur)} of new money this month goes to the lines furthest below target. Sales only
            when a line is more than {data.band_pp} pp above target and a year of contributions would not fix it:{" "}
            {data.sells ? "one is due." : "none is due."}
          </p>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-xs">
              <thead className="text-left text-text-muted">
                <tr>
                  <th className="py-1 font-normal">Line</th>
                  <th className="py-1 text-right font-normal">Now</th>
                  <th className="py-1 text-right font-normal">Target</th>
                  <th className="py-1 text-right font-normal">Drift</th>
                  <th className="py-1 text-right font-normal">Buy</th>
                  <th className="py-1 text-right font-normal">Sell</th>
                  <th className="py-1 text-right font-normal">Taxable gain</th>
                </tr>
              </thead>
              <tbody>
                {lines.map((r) => (
                  <tr key={r.key} className="border-t border-border/50">
                    <td className="py-1">
                      <span className="font-medium">{r.ticker ?? r.key}</span>{" "}
                      <span className="text-text-muted">{r.name}</span>
                    </td>
                    <td className="py-1 text-right font-mono tabular-nums">{pct(r.current_weight)}</td>
                    <td className="py-1 text-right font-mono tabular-nums">{pct(r.target_weight)}</td>
                    <td className={`py-1 text-right font-mono tabular-nums ${r.in_band ? "text-text-muted" : "text-warn"}`}>
                      {r.drift_pp > 0 ? "+" : ""}{r.drift_pp.toFixed(1)} pp
                    </td>
                    <td className="py-1 text-right font-mono tabular-nums text-success">{r.buy_eur > 0.5 ? eur(r.buy_eur) : "—"}</td>
                    <td className="py-1 text-right font-mono tabular-nums text-danger">{r.sell_eur > 0.5 ? eur(r.sell_eur) : "—"}</td>
                    <td className="py-1 text-right font-mono tabular-nums">
                      {r.sell_eur > 0.5 ? (r.taxable_gain_eur == null ? "cost unknown" : eur(r.taxable_gain_eur)) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.sells && (
            <p className="mt-2 text-xs text-text-secondary">
              Flat-rate tax on the sales: {eur(data.tax_eur)} before your allowance or NV certificate (Tax shows what is
              actually left).
            </p>
          )}
          {(data.unpriced?.length ?? 0) > 0 && (
            <p className="mt-1 text-xs text-text-muted">Left as they are (no price history): {data.unpriced!.join(", ")}.</p>
          )}
          <p className="mt-2 text-[10px] text-text-muted">
            Informational only, nothing is ordered. Your actual plan, with its fees and brokers, is on This month.
          </p>
        </>
      )}
    </section>
  );
}
