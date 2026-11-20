import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type DerivativePosition, type DerivativeTerms, type DerivativesResponse } from "../../../lib/api";
import { formatCurrency, formatDate, formatPercent } from "../../../lib/format";
import { Button } from "../../../components/ui/button";
import { Input } from "../../../components/ui/input";
import { Label } from "../../../components/ui/label";
import { Table } from "../../../components/ui/table";

const eur = (v?: number | null, digits = 2) => formatCurrency(v, "EUR", { digits });
const pct = (v?: number | null, digits = 0) => formatPercent(v, { digits });

/**
 * Warrants the brokers report (ADR 0026). The broker feed names a warrant but
 * leaves out its expiry, ratio and strike currency; the owner enters them once
 * and the card shows what the price implies. Renders nothing without warrants.
 */
export function DerivativesCard() {
  const q = useQuery({ queryKey: ["derivatives"], queryFn: () => api<DerivativesResponse>("/api/derivatives") });
  const positions = q.data?.positions;
  if (q.isError || !Array.isArray(positions) || positions.length === 0) return null;
  return (
    <section aria-labelledby="derivatives-heading" className="space-y-3 rounded-md border border-border p-3 text-sm">
      <h2 id="derivatives-heading" className="text-base font-semibold text-text-primary">Warrants and options</h2>
      <p className="text-xs text-text-muted">
        Short-term instruments: a warrant loses its time value as expiry nears and can expire worthless. The
        figures are model estimates from the broker's price and the underlying's last close; the issuer's price
        includes its margin. Quantfolio tracks these positions but does not recommend options.
      </p>
      {positions.map((p) => (
        <DerivativeRow key={p.isin} p={p} />
      ))}
    </section>
  );
}

function DerivativeRow({ p }: { p: DerivativePosition }) {
  const [editing, setEditing] = useState(false);
  const a = p.analysis;
  const t = p.terms;
  return (
    <div className="space-y-2 rounded-md border border-border p-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <div className="font-medium text-text-primary">{p.name}</div>
          <div className="text-xs text-text-muted">
            {p.isin} · {p.quantity} × {eur(p.price_eur, 3)} = {eur(p.value_eur)}
            {p.pnl_eur != null && (
              <span className={p.pnl_eur < 0 ? "text-danger" : "text-success"}>
                {" "}({p.pnl_eur >= 0 ? "+" : ""}{eur(p.pnl_eur)})
              </span>
            )}
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={() => setEditing((v) => !v)}>
          {t ? "Edit terms" : "Enter terms"}
        </Button>
      </div>
      {!t && !editing && (
        <p className="text-xs text-text-muted">
          Enter the expiry, ratio and strike currency (from the issuer's product page or the order confirmation) to
          see time decay, break-even and the expiry warning.
        </p>
      )}
      {editing && <TermsForm p={p} onDone={() => setEditing(false)} />}
      {t && a && (
        <div className="space-y-2">
          <p className="text-xs text-text-muted">
            {t.kind === "put" ? "Put" : "Call"} on {t.underlying_symbol}, strike {t.strike} {t.strike_currency}, ratio{" "}
            {t.ratio}, {t.style}, expires {formatDate(t.expiry)}
            {a.expired ? " (expired)" : ` (${a.days_to_expiry} days)`}
            {a.spot != null && ` · ${t.underlying_symbol} ${a.spot.toFixed(2)} ${t.strike_currency}`}
            {a.spot_day && ` on ${formatDate(a.spot_day)}`}
          </p>
          {a.missing.length > 0 && (
            <p className="text-xs text-text-muted">Not shown: {a.missing.join(", ")}.</p>
          )}
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
            <Fact label="Implied volatility" value={pct(a.implied_vol)} hint="The volatility the price implies (the issuer's margin included)." />
            <Fact label="Moves like" value={eur(a.exposure_eur, 0)} hint={`Delta ${a.delta?.toFixed(2) ?? "–"}: the position gains or loses like this much of ${t.underlying_symbol} (negative: it gains when ${t.underlying_symbol} falls).`} />
            <Fact label="Time decay per day" value={eur(a.theta_eur_per_day)} hint="What the position loses in a day if nothing else changes." />
            <Fact label="Break-even at expiry" value={a.break_even != null ? `${a.break_even.toFixed(2)} ${t.strike_currency} (${pct(a.break_even_move, 1)})` : "–"} hint="Where the underlying must be at expiry to get the price paid today back." />
            <Fact label="Time value" value={eur(a.time_value_eur != null ? a.time_value_eur * p.quantity : null)} hint="The part of the price above what exercising now pays; it goes to zero by expiry." />
            <Fact label="Chance to end in the money" value={pct(a.prob_in_the_money)} hint="Under the same model; a model estimate, not a forecast." />
            {p.listed && (
              <Fact label="Listed options' volatility" value={pct(p.listed.implied_vol)} hint={`Yahoo, strike ${p.listed.strike ?? "–"}, expiry ${p.listed.expiry ?? "–"}, on ${p.listed.day}. Below the warrant's: the issuer's margin.`} />
            )}
          </dl>
          {a.scenarios && a.scenarios.length > 0 && (
            <details>
              <summary className="cursor-pointer text-xs text-text-muted">Value at expiry if {t.underlying_symbol} moves…</summary>
              <div className="overflow-x-auto">
                <Table className="w-full text-left text-xs tabular-nums">
                  <thead className="text-text-muted">
                    <tr>
                      <th className="pr-2 font-normal">Move</th>
                      <th className="pr-2 font-normal">{t.underlying_symbol}</th>
                      <th className="text-right font-normal">Worth at expiry</th>
                    </tr>
                  </thead>
                  <tbody>
                    {a.scenarios.map((s) => (
                      <tr key={s.move}>
                        <td className="pr-2">{s.move > 0 ? "+" : ""}{pct(s.move)}</td>
                        <td className="pr-2">{s.underlying.toFixed(2)}</td>
                        <td className="text-right">{eur(s.value_eur)}</td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </div>
            </details>
          )}
        </div>
      )}
    </div>
  );
}

function Fact({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div title={hint}>
      <dt className="text-text-muted">{label}</dt>
      <dd className="tabular-nums text-text-primary">{value}</dd>
    </div>
  );
}

function TermsForm({ p, onDone }: { p: DerivativePosition; onDone: () => void }) {
  const queryClient = useQueryClient();
  const start: DerivativeTerms = p.terms ?? {
    kind: p.parsed?.kind ?? "put",
    style: "american",
    strike: p.parsed?.strike ?? 0,
    strike_currency: "USD",
    ratio: 0.1,
    expiry: "",
    underlying_symbol: "",
    issuer: p.parsed?.issuer ?? null,
  };
  const [v, setV] = useState(start);
  const save = useMutation({
    mutationFn: () => api(`/api/derivatives/${p.isin}/terms`, { method: "PUT", body: JSON.stringify(v) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["derivatives"] });
      toast.success("Terms saved");
      onDone();
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const field = (key: keyof DerivativeTerms, label: string, type = "text", step?: string) => (
    <div className="space-y-1">
      <Label htmlFor={`${p.isin}-${key}`}>{label}</Label>
      <Input
        id={`${p.isin}-${key}`}
        type={type}
        step={step}
        value={(v[key] as string | number | null | undefined) ?? ""}
        onChange={(e) => setV({ ...v, [key]: type === "number" ? Number(e.target.value) : e.target.value })}
      />
    </div>
  );
  return (
    <form
      className="grid grid-cols-2 gap-2 sm:grid-cols-4"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <div className="space-y-1">
        <Label htmlFor={`${p.isin}-kind`}>Type</Label>
        <select id={`${p.isin}-kind`} className="h-9 w-full rounded-md border border-border bg-transparent px-2"
          value={v.kind} onChange={(e) => setV({ ...v, kind: e.target.value as "put" | "call" })}>
          <option value="put">Put</option>
          <option value="call">Call</option>
        </select>
      </div>
      <div className="space-y-1">
        <Label htmlFor={`${p.isin}-style`}>Exercise</Label>
        <select id={`${p.isin}-style`} className="h-9 w-full rounded-md border border-border bg-transparent px-2"
          value={v.style} onChange={(e) => setV({ ...v, style: e.target.value as "american" | "european" })}>
          <option value="american">American</option>
          <option value="european">European</option>
        </select>
      </div>
      {field("underlying_symbol", "Underlying ticker (e.g. ANT)")}
      {field("expiry", "Expiry", "date")}
      {field("strike", "Strike", "number", "any")}
      {field("strike_currency", "Strike currency")}
      {field("ratio", "Ratio (shares per warrant)", "number", "any")}
      <div className="flex items-end">
        <Button type="submit" size="sm" disabled={save.isPending || !v.expiry || !v.underlying_symbol || !(v.ratio > 0) || !(v.strike > 0)}>
          {save.isPending ? "Saving..." : "Save terms"}
        </Button>
      </div>
    </form>
  );
}
