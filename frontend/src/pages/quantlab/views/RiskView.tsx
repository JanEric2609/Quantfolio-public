import { useSearchParams } from "react-router-dom";
import { RegressionScatter } from "../components/RegressionScatter";
import { ReturnHistogram } from "../components/ReturnHistogram";
import { RollingBetaChart } from "../components/RollingBetaChart";
import { useQuantRisk } from "../hooks/useQuantRisk";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";
import { formatMetricNumber, formatMetricPercent } from "../../../components/composed/MetricTile";
import type { QuantRiskMetrics, QuantRiskSeries } from "../../../lib/api";

const num = (value: number | null | undefined) => formatMetricNumber(value, 2, "-");
const pct = (value: number | null | undefined) => formatMetricPercent(value, 2, "-");

/**
 * Risk of the whole book (DKB and Scalable, in EUR) against a benchmark in
 * EUR, both paired by date. Empty fields mean the defaults: the configured
 * benchmark (MSCI World EUR) and the ECB deposit rate.
 */
export function RiskView() {
  const [params, setParams] = useSearchParams();
  const benchmark = params.get("benchmark") ?? "";
  const confidence = params.get("confidence") ?? "95";
  const riskFree = params.get("riskFree") ?? "";

  const riskQuery = useQuantRisk(benchmark, confidence, riskFree);
  const risk: Partial<
    QuantRiskMetrics & QuantRiskSeries & {
      historical_var: number; parametric_var: number; insufficient_history: boolean; risk_free: number;
    }
  > = riskQuery.data?.risk ?? {};
  const usedBenchmark = riskQuery.data?.benchmark ?? "";
  const diag = riskQuery.data?.diagnostics ?? {};
  const short = risk.insufficient_history === true;
  const shortHint = short ? `${risk.samples ?? 0} days; a year is needed` : undefined;
  const benchDays = risk.benchmark_samples ?? 0;
  const noBench = riskQuery.data?.available && diag.benchmark_available === false;
  const alphaT = risk.alpha_t_stat;

  const overlapPct = riskQuery.data?.benchmark_overlap_pct ?? null;
  const overlapCaveat = riskQuery.data?.available === true
    && ((overlapPct != null && overlapPct >= 80) || (risk.r_squared != null && risk.r_squared >= 0.95));

  const set = (key: string, value: string) =>
    setParams((old) => {
      if (value) old.set(key, value);
      else old.delete(key);
      return old;
    });

  const metrics: MetricItem[] = [
    { label: `VaR 1 day (${confidence}%)`, value: pct(risk.historical_var), metricKey: `var_${confidence}`, metricValue: risk.historical_var },
    { label: `CVaR 1 day (${confidence}%)`, value: pct(risk.historical_cvar), metricKey: `cvar_${confidence}`, metricValue: risk.historical_cvar },
    { label: "Volatility (yearly)", value: pct(risk.annualised_volatility), metricKey: "annualised_volatility", metricValue: risk.annualised_volatility },
    { label: "Sharpe", value: short ? "—" : num(risk.sharpe), metricKey: "sharpe", metricValue: short ? undefined : risk.sharpe, hint: shortHint },
    { label: "Sortino", value: short ? "—" : num(risk.sortino), metricKey: "sortino", metricValue: short ? undefined : risk.sortino, hint: shortHint },
    {
      label: "Beta", value: num(risk.beta), metricKey: "beta", metricValue: risk.beta ?? undefined,
      hint: risk.beta_dimson != null ? `lead/lag-adjusted ${risk.beta_dimson.toFixed(2)}` : undefined,
    },
    {
      label: "Alpha (yearly)", value: pct(risk.alpha), metricKey: "alpha", metricValue: risk.alpha ?? undefined,
      hint: alphaT != null ? `t = ${alphaT.toFixed(2)}${Math.abs(alphaT) < 2 ? ", not significant" : ""}` : undefined,
    },
    { label: "R²", value: num(risk.r_squared), metricKey: "r_squared", metricValue: risk.r_squared ?? undefined },
    { label: "Tracking error", value: pct(risk.tracking_error), metricKey: "tracking_error", metricValue: risk.tracking_error ?? undefined },
    { label: "Calmar", value: short ? "—" : num(risk.calmar), metricKey: "calmar", metricValue: short ? undefined : risk.calmar, hint: shortHint },
  ];

  return (
    <div className="space-y-0">
      <SummaryStrip metrics={metrics} />

      <div className="space-y-4 pt-4">
        {overlapCaveat && (
          <div className="rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn" data-testid="benchmark-caveat">
            {overlapPct != null ? `${overlapPct.toFixed(0)} %` : "Most"} of your book is the benchmark fund itself, so beta ≈ 1,
            R² ≈ 1 and tracking error ≈ 0 by construction; they say nothing about skill. The useful numbers here are
            volatility, drawdown and VaR.
          </div>
        )}
        {riskQuery.data?.available && diag.partial_data && (
          <div className="rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
            Partial portfolio data: some holdings have no price history. Risk uses{" "}
            {diag.priced_assets?.length ?? 0} of {(diag.priced_assets?.length ?? 0) + (diag.missing_history?.length ?? 0)} lines.
          </div>
        )}
        {riskQuery.data?.available && (diag.missing_fx?.length ?? 0) > 0 && (
          <div className="rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
            No exchange rate for {diag.missing_fx.join(", ")}; left out rather than mixing currencies.
          </div>
        )}
        {noBench && (
          <div className="rounded-md border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
            {usedBenchmark || "The benchmark"} shares only {benchDays} trading days with the book; beta, alpha and R² need 40.
          </div>
        )}

        <section className="flex flex-wrap items-center gap-3 rounded-md p-3 text-xs bg-surface">
          <label className="flex items-center gap-1">
            <span className="text-text-muted">Benchmark</span>
            <input
              value={benchmark}
              placeholder={usedBenchmark || "EUNL.DE"}
              onChange={(e) => set("benchmark", e.target.value.toUpperCase())}
              className="w-28 rounded border border-line bg-surface px-2 py-1 uppercase"
              style={{ fontFamily: '"JetBrains Mono", monospace' }}
            />
          </label>
          <label className="flex items-center gap-1">
            <span className="text-text-muted">Confidence</span>
            <select
              value={confidence}
              onChange={(e) => set("confidence", e.target.value)}
              className="rounded border border-line bg-surface px-2 py-1"
            >
              <option>95</option>
              <option>99</option>
            </select>
          </label>
          <label className="flex items-center gap-1">
            <span className="text-text-muted">Risk-free (% a year)</span>
            <input
              value={riskFree}
              inputMode="decimal"
              placeholder={risk.risk_free != null ? (risk.risk_free * 100).toFixed(2) : "ECB"}
              onChange={(e) => set("riskFree", e.target.value)}
              className="w-16 rounded border border-line bg-surface px-2 py-1"
            />
          </label>
          <span className="text-text-muted">
            All returns in EUR, paired by date{benchDays ? ` (${benchDays} common days)` : ""}. The history holds today's
            weights constant, as if you had always owned this mix. Empty fields use the configured benchmark and the ECB
            deposit rate.
          </span>
        </section>

        <section className="rounded-md p-2 bg-surface">
          <h3 className="px-2 pt-1 text-xs font-medium text-text-secondary">
            Each dot is one trading day. The dashed line's slope is the beta.
          </h3>
          <RegressionScatter data={risk.regression_points ?? []} benchmark={usedBenchmark} />
        </section>

        <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          <section className="rounded-md p-2 bg-surface">
            <h3 className="px-2 pt-1 text-xs font-medium text-text-secondary">
              Beta over the trailing 63 trading days (about three months)
            </h3>
            <RollingBetaChart timeline={risk.rolling_beta ?? []} />
          </section>
          <section className="rounded-md p-2 bg-surface">
            <h3 className="px-2 pt-1 text-xs font-medium text-text-secondary">How large the daily moves were</h3>
            <ReturnHistogram values={risk.returns ?? []} />
          </section>
        </div>
      </div>
    </div>
  );
}
