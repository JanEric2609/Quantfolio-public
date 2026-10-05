import { KpiTile } from "../../../components/composed/KpiTile";
import { MetricGroup } from "../../../components/composed/MetricGroup";
import { formatCurrency } from "../../../lib/format";
import type { TaxOverview } from "../../../lib/api";

/**
 * What this year's capital income really costs. The flat-rate figures of the
 * overview (25 % on everything above one allowance) are what a bank without
 * an NV copy would withhold, not what is owed; the position block nets in the
 * NV certificate, each bank's Freistellungsauftrag and the Günstigerprüfung.
 */
export function TaxKpiStrip({ overview }: { overview: TaxOverview }) {
  const p = overview.position;
  const eur = (v: number | undefined | null) => formatCurrency(v ?? 0, "EUR");
  if (!p?.applicable) {
    return (
      <MetricGroup>
        <KpiTile label="Tax at the flat rate" value={eur(overview.tax.total_tax_due_eur)} tone="warn" />
        <KpiTile label="Vorabpauschale (taxable)" value={eur(overview.lines.vorabpauschale_taxable_eur)} glossaryKey="vorabpauschale" />
      </MetricGroup>
    );
  }
  const refund = p.refund_with_return_eur ?? 0;
  const owed = p.final_tax_eur ?? 0;
  return (
    <div className="space-y-3">
      <div
        className={`rounded-lg border p-3 text-sm ${owed === 0 && refund === 0 ? "border-success/30 bg-success/10 text-success" : "border-info/30 bg-info/10 text-text-primary"}`}
        data-testid="tax-headline"
      >
        {p.headline}
      </div>
      <MetricGroup>
        <KpiTile label="Tax owed for the year" value={eur(owed)} tone={owed > 0 ? "warn" : "good"} />
        <KpiTile label="Withheld by your banks" value={eur(p.withheld_by_banks_eur)} tone={(p.withheld_by_banks_eur ?? 0) > 0 ? "warn" : "neutral"} />
        <KpiTile label="Back with a tax return" value={eur(refund)} tone={refund > 0 ? "good" : "neutral"} />
        <KpiTile label="Capital income (projected)" value={eur(p.projected_capital_income_eur)} />
        <KpiTile label="Vorabpauschale (taxable)" value={eur(overview.lines.vorabpauschale_taxable_eur)} glossaryKey="vorabpauschale" />
      </MetricGroup>
      <p className="text-xs text-text-muted">
        {p.use_guenstigerpruefung
          ? "The personal tariff is lower than the flat 25 % here, so the return should tick the Günstigerprüfung in Anlage KAP."
          : "The flat 25 % (Abgeltungsteuer) is the final tax here."}{" "}
        Projected income includes the interest, dividends and January Vorabpauschale still expected this year.
      </p>
    </div>
  );
}
