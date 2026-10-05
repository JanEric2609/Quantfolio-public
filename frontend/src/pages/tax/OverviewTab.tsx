import { useOutletContext } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type TaxOverview } from "../../lib/api";
import { formatCurrency, formatPercent } from "../../lib/format";
import { TaxKpiStrip } from "./components/TaxKpiStrip";
import { TaxLinesWaterfall } from "./components/TaxLinesWaterfall";
import { YearTrendChart } from "./components/YearTrendChart";
import { AllowancePlanCard } from "./components/AllowancePlanCard";
import { GainHarvestCard } from "./components/GainHarvestCard";
import { MetricGroup } from "../../components/composed/MetricGroup";
import { KpiTile } from "../../components/composed/KpiTile";

export function OverviewTab() {
  const { year } = useOutletContext<{ year: number }>();

  const overview = useQuery({
    queryKey: ["tax", "overview", year],
    queryFn: () => api<TaxOverview>(`/api/tax/overview?year=${year}`),
  });

  if (overview.isLoading) {
    return <div className="text-sm text-text-secondary">Computing overview…</div>;
  }

  if (!overview.data) {
    return <div className="text-sm text-danger">Failed to load overview.</div>;
  }

  const o = overview.data;
  const provenance = o.provenance.events_by_source ?? {};

  return (
    <div className="space-y-6">
      <TaxKpiStrip overview={o} />

      <AllowancePlanCard year={year} />

      <GainHarvestCard year={year} />

      {o.vorabpauschale_estimate?.estimated_from_holdings && (
        <div className="rounded-lg border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
          Vorabpauschale is auto-estimated from your current fund/ETF holdings
          ({o.vorabpauschale_estimate.items.length} fund
          {o.vorabpauschale_estimate.items.length === 1 ? "" : "s"}, Basiszins{" "}
          {o.vorabpauschale_estimate.items[0]?.basiszins != null
            ? formatPercent(o.vorabpauschale_estimate.items[0].basiszins, { digits: 2 })
            : "—"}
          ) because no Vorabpauschale events were recorded for {o.tax_year}. Estimate
          only — your broker's Steuerbescheinigung is authoritative.
        </div>
      )}

      <TaxLinesWaterfall overview={o} />

      <YearTrendChart year={year} />

      <MetricGroup title="Anlage KAP mapping (estimate)">
        {Object.entries(o.anlage_kap_mapping).map(([k, v]) => (
          <KpiTile
            key={k}
            label={k}
            value={typeof v === "number" ? formatCurrency(v, "EUR") : String(v)}
            glossaryKey={k.toLowerCase().replace(/\s+/g, "_")}
          />
        ))}
      </MetricGroup>

      <div className="rounded-lg border border-border bg-surface p-4">
        <div className="mb-2 text-sm font-medium text-text-primary">Provenance</div>
        <div className="text-xs text-text-secondary">
          {o.provenance.events_total} events ·
          {Object.entries(provenance).length > 0
            ? Object.entries(provenance).map(([src, count]) => ` ${src}=${count}`).join(" ·")
            : " no events yet"}
        </div>
      </div>
    </div>
  );
}
