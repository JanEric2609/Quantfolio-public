import { Card, CardContent, CardHeader, CardTitle } from "../../../../components/ui/card";
import { Badge } from "../../../../components/ui/badge";
import { BarChart } from "../../../../components/charts/BarChart";
import { DonutChart } from "../../../../components/charts/DonutChart";
import { Skeleton } from "../../../../components/ui/skeleton";
import type { RiskAssessment, VerificationStressResult } from "../../../../lib/api";

const pct = (v: number, digits = 1) => `${(v * 100).toFixed(digits)} %`;

function concentrationLabel(hhi: number): { label: string; variant: "success" | "info" | "warning" | "danger" } {
  if (hhi < 0.15) return { label: "Well diversified", variant: "success" };
  if (hhi < 0.25) return { label: "Moderate", variant: "info" };
  if (hhi < 0.5) return { label: "Concentrated", variant: "warning" };
  return { label: "Extreme", variant: "danger" };
}

function ConcentrationBar({ hhi, label }: { hhi: number; label: string }) {
  const meta = concentrationLabel(hhi);
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-xs">
        <span className="text-text-secondary">{label}</span>
        <Badge variant={meta.variant}>{meta.label}</Badge>
      </div>
      <div className="h-2.5 overflow-hidden rounded-full bg-surface-2" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={1} aria-valuenow={Number(hhi.toFixed(3))}>
        <div className="h-full rounded-full bg-accent" style={{ width: `${Math.min(hhi * 100, 100)}%` }} />
      </div>
      <div className="flex justify-between font-mono text-[11px] text-text-muted">
        <span>0 diversified</span>
        <span className="tabular-nums">{hhi.toFixed(3)}</span>
        <span>1 single asset</span>
      </div>
    </div>
  );
}

/** Holding-level HHI, and the look-through HHI once index ETFs are opened up. */
export function ConcentrationCard({ risk }: { risk: RiskAssessment }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm font-medium text-text-secondary">Concentration</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <ConcentrationBar hhi={risk.concentration_index} label="By holding (HHI)" />
        {risk.lookthrough_count > 0 ? (
          <>
            <ConcentrationBar hhi={risk.lookthrough_hhi} label="Look-through (ETFs opened up)" />
            <p className="text-xs text-text-secondary">
              About {risk.lookthrough_count} underlying positions once your ETFs are looked through. This is why an ETF-heavy
              portfolio is far less concentrated than its holding list suggests.
            </p>
          </>
        ) : (
          <p className="text-xs text-text-muted">No ETF look-through data for these holdings yet.</p>
        )}
      </CardContent>
    </Card>
  );
}

/** Currency the value really moves with: index ETFs are looked through to the currencies their index trades in. */
export function CurrencyCard({ risk }: { risk: RiskAssessment }) {
  const slices = Object.entries(risk.currency_exposure)
    .map(([name, value]) => ({ name, value: Math.round(value * 1000) / 10 }))
    .sort((a, b) => b.value - a.value);
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm font-medium text-text-secondary">Currency exposure</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {slices.length > 0 ? (
          <>
            <BarChart
              categories={slices.map((s) => s.name)}
              series={[{ name: "Share of portfolio (%)", data: slices.map((s) => s.value) }]}
              horizontal
              height={Math.max(140, slices.length * 34 + 40)}
              ariaLabel="Currency exposure"
            />
            <ul className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs" aria-label="Currency exposure values">
              {slices.map((s) => (
                <li key={s.name} className="flex justify-between">
                  <span className="text-text-secondary">{s.name}</span>
                  <span className="font-mono tabular-nums text-text-primary">{pct(s.value / 100)}</span>
                </li>
              ))}
            </ul>
            <p className="text-[11px] text-text-muted">
              Counts the currencies inside index ETFs: a euro-quoted MSCI World ETF is still mostly a dollar exposure.
            </p>
          </>
        ) : (
          <p className="py-6 text-center text-sm text-text-muted">No currency data yet.</p>
        )}
      </CardContent>
    </Card>
  );
}

/** Asset type (stock, ETF, bond, …) shares; not sectors. */
export function AssetTypeCard({ risk }: { risk: RiskAssessment }) {
  const slices = Object.entries(risk.asset_type_exposure)
    .map(([name, value]) => ({ name, value: Math.round(value * 100) }))
    .sort((a, b) => b.value - a.value);
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm font-medium text-text-secondary">Asset types</CardTitle>
      </CardHeader>
      <CardContent>
        {slices.length > 0 ? (
          <div className="flex flex-col items-center">
            <DonutChart slices={slices} height={200} innerRadius={50} ariaLabel="Asset type shares" />
            <ul className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs" aria-label="Asset type shares">
              {slices.map((s) => (
                <li key={s.name} className="flex justify-between gap-3">
                  <span className="truncate text-text-secondary">{s.name}</span>
                  <span className="font-mono tabular-nums text-text-muted">{s.value} %</span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="py-6 text-center text-sm text-text-muted">No holdings to split by type yet.</p>
        )}
      </CardContent>
    </Card>
  );
}

const eur = (v: number) =>
  new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR", maximumFractionDigits: 0 }).format(v);

/** What-if scenarios: a market drop and two currency moves. Illustrations of exposure, not forecasts. */
export function StressCard({ stress, loading }: { stress: VerificationStressResult | undefined; loading: boolean }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm font-medium text-text-secondary">What-if scenarios</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {loading ? (
          [0, 1, 2].map((i) => <Skeleton key={i} className="h-14 w-full" />)
        ) : !stress || stress.scenarios.length === 0 ? (
          <p className="py-4 text-center text-sm text-text-muted">No scenarios yet: they need at least one holding with a value.</p>
        ) : (
          <>
            <ul className="divide-y divide-border">
              {stress.scenarios.map((s) => {
                const down = s.portfolio_impact_pct < 0;
                return (
                  <li key={s.scenario_name} className="space-y-1 py-3 first:pt-0">
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="text-sm font-medium text-text-primary">{s.scenario_name}</span>
                      <span className={`font-mono text-sm font-semibold tabular-nums ${down ? "text-danger" : "text-success"}`}>
                        {s.portfolio_impact_pct > 0 ? "+" : ""}
                        {s.portfolio_impact_pct.toFixed(1)} %
                      </span>
                    </div>
                    <p className="text-xs text-text-secondary">{s.description}</p>
                    <p className="font-mono text-[11px] text-text-muted">Largest single-holding loss {eur(s.worst_case_loss)}</p>
                  </li>
                );
              })}
            </ul>
            <p className="text-[11px] italic text-text-muted">{stress.note}</p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
