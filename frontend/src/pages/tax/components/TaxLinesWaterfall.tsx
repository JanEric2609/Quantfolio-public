import { WaterfallChart } from "../../../components/charts/WaterfallChart";
import { formatCurrency } from "../../../lib/format";
import type { TaxOverview } from "../../../lib/api";

/**
 * From booked income to the flat-rate tax, every bar after Teilfreistellung
 * so the steps add up: taxable dividends, interest, net gains and the
 * Vorabpauschale, less the general loss pot and the allowance, give the
 * taxable income; the last bar is 25 % plus Soli and church tax on it. This
 * is what a bank without an NV copy would withhold; the tiles above show what
 * is owed.
 */
export function TaxLinesWaterfall({ overview }: { overview: TaxOverview }) {
  const l = overview.lines;
  const n = (k: string) => Number(l[k] ?? 0);
  const tax = overview.tax;
  const items = [
    { label: "Dividends (taxable)", delta: n("dividends_taxable_eur") },
    { label: "Interest", delta: n("interest_gross_eur") },
    { label: "Share gains (net)", delta: n("net_aktien_eur") },
    { label: "Other gains", delta: n("realised_gain_sonstige_eur") },
    { label: "Vorabpauschale", delta: n("vorabpauschale_taxable_eur") },
    { label: "Loss pot offset", delta: -n("general_loss_offset_eur") },
    { label: "Before allowance", delta: n("taxable_income_before_allowance_eur"), total: true },
    { label: "Sparer-Pauschbetrag", delta: -Number(overview.allowance?.used_now_eur ?? 0) },
    { label: "Taxable", delta: n("taxable_income_after_allowance_eur"), total: true },
    {
      label: "Flat-rate tax",
      delta: Number(tax.kest_after_foreign_wht_eur ?? tax.kest_due_eur ?? 0) + Number(tax.soli_eur ?? 0) + Number(tax.church_tax_eur ?? 0),
      total: true,
    },
  ];
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium text-text-secondary">From booked income to the flat-rate tax (booked so far)</h3>
      <WaterfallChart items={items} ariaLabel="Tax waterfall chart" height={320} yName="EUR" format={(v) => formatCurrency(v, "EUR")} />
      <p className="text-xs text-text-muted">
        Amounts after Teilfreistellung. The last bar is what the flat 25 % would take with one allowance; banks with
        an NV copy withhold nothing, and the Günstigerprüfung can lower it.
      </p>
    </div>
  );
}
