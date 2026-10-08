import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { formatCurrency, formatPercent } from "../../../lib/format";
import { api, type RealRebalanceResponse, type RebalanceDepot } from "../../../lib/api";

const pct = (v?: number | null) => formatPercent(v, { digits: 1 });
const eur = (v?: number | null) => (v == null ? "—" : formatCurrency(v, "EUR"));

/**
 * Inline form for a DKB position whose purchase cost did not come through the
 * bank connection: the owner types the "Einstandswert" the DKB app shows for it.
 */
function CostEntry({ depot }: { depot: RebalanceDepot }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const amount = Number(value.replace(",", "."));
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await api(`/api/dkb/positions/${encodeURIComponent(depot.position_id)}/cost`, {
        method: "PUT",
        body: JSON.stringify({ einstandswert_eur: amount }),
      });
      setOpen(false);
      await qc.invalidateQueries({ queryKey: ["quant", "portfolio", "real"] });
    } catch {
      setError("Could not save the cost.");
    } finally {
      setBusy(false);
    }
  };
  if (!open) {
    return (
      <button type="button" className="mt-0.5 block text-[11px] text-accent underline" onClick={() => setOpen(true)}>
        Cost unknown at {depot.broker} — enter Einstandswert
      </button>
    );
  }
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1 text-[11px]">
      <label className="flex items-center gap-1">
        <span className="text-text-muted">Einstandswert at {depot.broker} (EUR, total, as in the DKB app)</span>
        <input
          className="w-24 rounded border border-line bg-surface px-1 py-0.5 text-right"
          inputMode="decimal"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          aria-label={`Einstandswert ${depot.broker}`}
        />
      </label>
      <button
        type="button"
        className="rounded border border-line px-2 py-0.5 disabled:opacity-50"
        disabled={busy || !(amount > 0)}
        onClick={save}
      >
        Save
      </button>
      <button type="button" className="text-text-muted underline" onClick={() => setOpen(false)}>Cancel</button>
      {error && <span className="text-danger">{error}</span>}
    </div>
  );
}

function GainCell({ line }: { line: NonNullable<RealRebalanceResponse["lines"]>[number] }) {
  if (line.sell_eur <= 0.5) return <>—</>;
  if (line.taxable_gain_eur == null) return <>cost unknown</>;
  return (
    <>
      {eur(line.taxable_gain_eur)}
      {line.gain_complete === false && (
        <span className="block text-[10px] text-text-muted">
          without {(line.cost_unknown_depots ?? []).join(", ")} (cost unknown)
        </span>
      )}
      {line.gain_complete !== false && line.gain_basis === "fifo_estimated" && (
        <span className="block text-[10px] text-text-muted">unit counts estimated</span>
      )}
    </>
  );
}

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
    sleeves: "Your plan's sleeves", equal: "Equal weight (1/N)", inverse_vol: "Inverse volatility", min_variance: "Minimum variance",
    erc: "Equal risk contribution", hrp: "Hierarchical risk parity",
  };
  const lines = data?.lines ?? [];
  // Buys for a sleeve the book doesn't hold yet (no line row to carry them).
  const heldKeys = new Set(lines.map((l) => l.key));
  const unheldBuys = (data?.suggestions ?? []).filter(
    (sg) => sg.action === "buy" && sg.sleeve && !(sg.isin && heldKeys.has(sg.isin)),
  );
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
          {(data.sleeves?.length ?? 0) > 0 && (
            <div className="mb-3 overflow-x-auto">
              <table className="w-full min-w-[480px] text-xs" aria-label="Sleeves">
                <thead className="text-left text-text-muted">
                  <tr>
                    <th className="py-1 font-normal">Sleeve</th>
                    <th className="py-1 text-right font-normal">Now</th>
                    <th className="py-1 text-right font-normal">Target</th>
                    <th className="py-1 text-right font-normal">After</th>
                    <th className="py-1 text-right font-normal">Buy</th>
                    <th className="py-1 text-right font-normal">Sell</th>
                  </tr>
                </thead>
                <tbody>
                  {data.sleeves!.map((sl) => (
                    <tr key={sl.key} className="border-t border-border/50">
                      <td className="py-1">
                        <span className="font-medium">{sl.label}</span>
                        {!sl.unlocked && <span className="ml-1 text-text-muted">(locked: held, never bought or sold here)</span>}
                      </td>
                      <td className="py-1 text-right font-mono tabular-nums">{sl.current_pct.toFixed(1)} %</td>
                      <td className="py-1 text-right font-mono tabular-nums">{sl.target_pct.toFixed(1)} %</td>
                      <td className="py-1 text-right font-mono tabular-nums">{sl.after_pct == null ? "—" : `${sl.after_pct.toFixed(1)} %`}</td>
                      <td className="py-1 text-right font-mono tabular-nums text-success">{sl.buy_eur > 0.5 ? eur(sl.buy_eur) : "—"}</td>
                      <td className="py-1 text-right font-mono tabular-nums text-danger">{sl.sell_eur > 0.5 ? eur(sl.sell_eur) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
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
                      {(r.depots ?? []).filter((d) => d.can_enter_cost).map((d) => (
                        <CostEntry key={d.position_id} depot={d} />
                      ))}
                    </td>
                    <td className="py-1 text-right font-mono tabular-nums">{pct(r.current_weight)}</td>
                    <td className="py-1 text-right font-mono tabular-nums">{pct(r.target_weight)}</td>
                    <td className={`py-1 text-right font-mono tabular-nums ${r.in_band ? "text-text-muted" : "text-warn"}`}>
                      {r.drift_pp > 0 ? "+" : ""}{r.drift_pp.toFixed(1)} pp
                    </td>
                    <td className="py-1 text-right font-mono tabular-nums text-success">{r.buy_eur > 0.5 ? eur(r.buy_eur) : "—"}</td>
                    <td className="py-1 text-right font-mono tabular-nums text-danger">{r.sell_eur > 0.5 ? eur(r.sell_eur) : "—"}</td>
                    <td className="py-1 text-right font-mono tabular-nums">
                      <GainCell line={r} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {unheldBuys.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-text-secondary">
              {unheldBuys.map((sg, i) => (
                <li key={`${sg.ticker}-${i}`}>
                  Also buy {eur(sg.estimated_amount)} of <span className="font-medium">{sg.ticker}</span>
                  {sg.isin ? "" : ": choose a fund for this sleeve in Plan settings"}.
                </li>
              ))}
            </ul>
          )}
          {data.sells && (
            <p className="mt-2 text-xs text-text-secondary">
              Flat-rate tax on the sales: {eur(data.tax_eur)}
              {data.tax_complete === false ? " (incomplete: a depot has no cost basis, so the real tax is higher)" : ""}. The gain is after Teilfreistellung (the partial exemption
              for funds), before your Sparerpauschbetrag or NV certificate and before any Vorabpauschale already taxed
              (Tax shows what is actually left). An estimate, not tax advice.
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
