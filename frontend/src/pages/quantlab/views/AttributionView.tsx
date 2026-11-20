import { formatNumber, formatPercent } from "../../../lib/format";
import { useState } from "react";
import { Button } from "../../../components/ui/button";
import { GlossaryTooltip } from "../../../components/composed/GlossaryTooltip";
import { useBrinsonAttribution } from "../hooks/useAttribution";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";

const _today = new Date();
const _isoDate = (d: Date) => d.toISOString().slice(0, 10);
const _oneYearAgo = new Date(_today.getFullYear() - 1, _today.getMonth(), _today.getDate());

export function AttributionView() {
  // "main" resolves to the user's primary portfolio server-side, so no UUID paste is needed.
  const [portfolioId, setPortfolioId] = useState("main");
  const [benchmarkTicker, setBenchmarkTicker] = useState("EUNL.DE");
  const [dateFrom, setDateFrom] = useState(_isoDate(_oneYearAgo));
  const [dateTo, setDateTo] = useState(_isoDate(_today));

  const brinson = useBrinsonAttribution();

  const handleRunAttribution = async () => {
    if (!portfolioId) return;
    brinson.mutate({
      portfolio_id: portfolioId,
      benchmark_ticker: benchmarkTicker,
      date_from: dateFrom,
      date_to: dateTo,
    });
  };

  const result = brinson.data;
  const attribution = result?.attribution;

  const metrics: MetricItem[] = attribution?.brinson
    ? [
        {
          label: "Allocation",
          value: formatPercent(attribution.brinson.allocation_effect, { digits: 3 }),
          metricKey: "allocation_effect",
          metricValue: attribution.brinson.allocation_effect,
        },
        {
          label: "Selection",
          value: formatPercent(attribution.brinson.selection_effect, { digits: 3 }),
          metricKey: "selection_effect",
          metricValue: attribution.brinson.selection_effect,
        },
        {
          label: "Interaction",
          value: formatPercent(attribution.brinson.interaction_effect, { digits: 3 }),
          metricKey: "interaction_effect",
          metricValue: attribution.brinson.interaction_effect,
        },
      ]
    : [
        { label: "Allocation", value: "-", metricKey: "allocation_effect" },
        { label: "Selection", value: "-", metricKey: "selection_effect" },
        { label: "Interaction", value: "-", metricKey: "interaction_effect" },
      ];

  return (
    <div className="space-y-0">
      {/* Summary Strip */}
      <SummaryStrip metrics={metrics} />

      {/* Detail Zone */}
      <div className="space-y-4 pt-4">
        {/* Input Form */}
        <section
          className="rounded-md p-4"
          style={{ background: "rgb(var(--c-surface))" }}
        >
          <h2 className="mb-4 text-sm font-semibold text-text-primary">
            <GlossaryTooltip k="brinson">
              Attribution Analysis
            </GlossaryTooltip>
          </h2>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="text-xs text-text-secondary">
              Portfolio
              <input
                type="text"
                value={portfolioId}
                onChange={(e) => setPortfolioId(e.target.value)}
                placeholder="main"
                className="mt-1 w-full rounded-md border border-line bg-surface px-2 py-1.5 text-text-primary"
              />
              <span className="mt-1 block text-[10px] text-text-muted">
                "main" uses your primary portfolio. Paste a portfolio ID to override.
              </span>
            </label>

            <label className="text-xs text-text-secondary">
              Benchmark Ticker
              <input
                type="text"
                value={benchmarkTicker}
                onChange={(e) => setBenchmarkTicker(e.target.value)}
                className="mt-1 w-full rounded-md border border-line bg-surface px-2 py-1.5 text-text-primary"
                style={{ fontFamily: '"JetBrains Mono", monospace' }}
              />
            </label>

            <label className="text-xs text-text-secondary">
              From
              <input
                type="date"
                value={dateFrom}
                onChange={(e) => setDateFrom(e.target.value)}
                className="mt-1 w-full rounded-md border border-line bg-surface px-2 py-1.5 text-text-primary"
              />
            </label>

            <label className="text-xs text-text-secondary">
              To
              <input
                type="date"
                value={dateTo}
                onChange={(e) => setDateTo(e.target.value)}
                className="mt-1 w-full rounded-md border border-line bg-surface px-2 py-1.5 text-text-primary"
              />
            </label>
          </div>

          <Button
            onClick={handleRunAttribution}
            disabled={brinson.isPending || !portfolioId}
            className="mt-4"
          >
            {brinson.isPending ? "Running..." : "Run Attribution"}
          </Button>
        </section>

        {/* Results */}
        {attribution && (
          <div className="space-y-4">
            {/* Brinson Summary */}
            {attribution.brinson?.securities && (
              <section
                className="rounded-md p-4"
                style={{ background: "rgb(var(--c-surface))" }}
              >
                <h3 className="mb-3 text-[11px] uppercase font-semibold text-text-secondary">
                  <GlossaryTooltip k="brinson">
                    Brinson Decomposition
                  </GlossaryTooltip>
                </h3>

                <div className="space-y-1 text-xs">
                  {attribution.brinson.securities
                    .slice(0, 10)
                    .map((sec) => (
                      <div
                        key={sec.isin}
                        className="flex justify-between text-text-secondary"
                      >
                        <span>{sec.ticker || sec.isin}</span>
                        <span
                          className="text-text-primary"
                          style={{
                            fontFamily: '"JetBrains Mono", monospace',
                          }}
                        >
                          {formatPercent(sec.total_effect, { digits: 3 })}
                        </span>
                      </div>
                    ))}
                </div>
              </section>
            )}

            {/* Factor Attribution */}
            {attribution.factor_contribution && (
              <section
                className="rounded-md p-4"
                style={{ background: "rgb(var(--c-surface))" }}
              >
                <h3 className="mb-3 text-[11px] uppercase font-semibold text-text-secondary">
                  <GlossaryTooltip k="factor_attribution">
                    Factor Contribution
                  </GlossaryTooltip>
                </h3>

                <div className="space-y-2">
                  {attribution.factor_contribution.factors.map((f) => (
                    <div
                      key={f.name}
                      className="flex justify-between text-xs"
                    >
                      <span className="text-text-secondary">{f.name}</span>
                      <span
                        className="text-text-primary"
                        style={{
                          fontFamily: '"JetBrains Mono", monospace',
                        }}
                      >
                        {f.contribution > 0 ? "+" : ""}
                        {formatPercent(f.contribution, { digits: 3 })}
                      </span>
                    </div>
                  ))}
                </div>

                <div className="mt-3 border-t border-line pt-2 text-xs">
                  <div className="flex justify-between">
                    <span className="text-text-secondary">R²</span>
                    <span
                      className="text-text-primary"
                      style={{
                        fontFamily: '"JetBrains Mono", monospace',
                      }}
                    >
                      {formatNumber(attribution.factor_contribution.r_squared, { digits: 3 })}
                    </span>
                  </div>
                </div>
              </section>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
