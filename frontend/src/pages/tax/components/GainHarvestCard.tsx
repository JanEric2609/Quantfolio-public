import { useQuery } from "@tanstack/react-query";
import { api, type GainHarvest, type GainHarvestStep } from "../../../lib/api";
import { formatCurrency, formatNumber } from "../../../lib/format";

const fmtQty = (q: number) => formatNumber(q, { digits: 4, minDigits: 0 });

/**
 * Gain harvesting: realise gains tax-free and buy the units straight back.
 * With an NV certificate the room is the Grundfreibetrag plus the
 * Sparer-Pauschbetrag; without one it is what each bank's Freistellungsauftrag
 * still covers. Hidden until one of the two is set; estimate only.
 */
export function GainHarvestCard({ year }: { year: number }) {
  const harvest = useQuery({
    queryKey: ["tax", "harvest", year],
    queryFn: () => api<GainHarvest>(`/api/tax/harvest?year=${year}`),
  });
  const h = harvest.data;
  if (!h || !h.enabled || !h.room) return null;

  const mode = h.mode ?? h.room.mode ?? "nv";
  const bankRooms = h.bank_rooms ?? [];
  const bankLabel = (step: GainHarvestStep) =>
    bankRooms.find((b) => b.bank === step.bank)?.label ?? step.bank ?? null;
  // Per-bank caps that actually limit this plan (null = no cap at that bank).
  const cappedRooms = bankRooms.filter((b) => b.room_eur !== null);
  const used = h.room.capital_income_so_far_eur + (h.room.other_income_eur ?? 0);

  return (
    <div className="space-y-3 rounded-lg border border-border bg-surface p-4">
      <div>
        <div className="text-sm font-medium text-text-primary">
          {mode === "allowance"
            ? "Gain harvesting within your Freistellungsauftrag"
            : "Tax-free gain harvesting (NV certificate)"}
        </div>
        <p className="mt-1 text-xs text-text-secondary">
          {mode === "allowance"
            ? "Without an NV certificate, only what each bank's Freistellungsauftrag still covers is untaxed this year, after the interest and dividends still expected. "
            : "Capital income up to the Grundfreibetrag plus the Sparer-Pauschbetrag is untaxed this year. "}
          Selling units with a gain and buying them straight back realises that gain tax-free and raises your cost
          basis, so it is never taxed later. Sales are always oldest-first (FIFO).
        </p>
      </div>

      <div className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-3">
        <div className="rounded-md bg-surface-2 p-2">
          <div className="text-text-secondary">Room left in {h.tax_year}</div>
          <div className="text-base font-medium text-text-primary">{formatCurrency(h.room.room_eur, "EUR")}</div>
        </div>
        <div className="rounded-md bg-surface-2 p-2">
          <div className="text-text-secondary">Already used</div>
          <div className="text-base text-text-primary">{formatCurrency(used, "EUR")}</div>
          {mode === "allowance" && h.room.projected_capital_income_eur != null ? (
            <div className="text-text-secondary">
              {formatCurrency(h.room.projected_capital_income_eur, "EUR")} expected by year end
            </div>
          ) : null}
        </div>
        <div className="rounded-md bg-surface-2 p-2">
          <div className="text-text-secondary">Tax avoided later</div>
          <div className="text-base text-text-primary">{formatCurrency(h.totals?.future_tax_avoided_eur ?? 0, "EUR")}</div>
        </div>
      </div>

      {cappedRooms.length > 0 && (
        <ul className="space-y-0.5 text-xs text-text-secondary">
          {cappedRooms.map((b) => (
            <li key={b.bank}>
              {b.label}: {formatCurrency(b.room_eur, "EUR")} left within its Freistellungsauftrag
            </li>
          ))}
        </ul>
      )}

      {h.steps.length === 0 ? (
        <div className="text-xs text-text-secondary">Nothing worth harvesting right now.</div>
      ) : (
        <ul className="space-y-2">
          {h.steps.map((s, i) => {
            const bank = bankLabel(s);
            return (
              <li key={`${s.isin}-${s.bank ?? i}`} className="rounded-md border border-border p-2 text-xs">
                <div className="font-medium text-text-primary">
                  Sell {s.whole_position ? "all" : fmtQty(s.sell_quantity)} {s.name ?? s.isin}
                  {bank ? ` at ${bank}` : ""}, buy the same number back
                </div>
                <div className="text-text-secondary">
                  about {formatCurrency(s.notional_eur, "EUR")} · gain {formatCurrency(s.gain_eur, "EUR")} (taxable{" "}
                  {formatCurrency(s.taxable_gain_eur, "EUR")}) · costs {formatCurrency(s.trading_cost_eur, "EUR")} · saves about{" "}
                  {formatCurrency(s.future_tax_avoided_eur, "EUR")} of tax later
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {h.skipped.length > 0 && (
        <div className="text-xs text-text-secondary">
          Not considered: {h.skipped.map((s) => `${s.name ?? s.isin} (${s.reason})`).join("; ")}
        </div>
      )}

      {h.warnings.length > 0 && (
        <ul className="space-y-1 rounded-md border border-warn/40 bg-warn/10 p-2 text-xs text-warn">
          {h.warnings.map((w) => (
            <li key={w.code}>{w.message}</li>
          ))}
        </ul>
      )}
      <div className="text-[11px] text-text-secondary">Estimate only, not tax advice.</div>
    </div>
  );
}
