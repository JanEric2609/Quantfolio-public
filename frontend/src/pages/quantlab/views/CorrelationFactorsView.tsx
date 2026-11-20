import { formatNumber } from "../../../lib/format";
import { useEffect, useState } from "react";
import { HeatmapChart } from "../../../components/charts/HeatmapChart";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../../components/ui/tabs";
import { Skeleton } from "../../../components/ui/skeleton";
import { EmptyState } from "../../../components/composed/EmptyState";
import { RollingBetaChart } from "../components/RollingBetaChart";
import { useCorrelationMatrix, useRollingCorrelation } from "../hooks/useCorrelationMatrix";
import { useQuantFactors } from "../hooks/useQuantFactors";
import { SummaryStrip } from "../components/SummaryStrip";
import type { MetricItem } from "../components/SummaryStrip";
import { MetricTooltip } from "../../../components/composed/MetricTooltip";
import { XCircle, Grid3x3 } from "lucide-react";

export function CorrelationFactorsView() {
  const corr = useCorrelationMatrix();
  const factors = useQuantFactors();
  const [pair, setPair] = useState<[string, string]>();
  useEffect(() => {
    if (!pair && (corr.data?.assets?.length ?? 0) > 1)
      setPair([corr.data.assets[0], corr.data.assets[1]]);
  }, [corr.data, pair]);
  const rolling = useRollingCorrelation(pair?.[0], pair?.[1]);

  if (corr.isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (corr.error) {
    return (
      <EmptyState
        icon={XCircle}
        title="Failed to load correlation data"
        body={corr.error instanceof Error ? corr.error.message : "An unexpected error occurred."}
      />
    );
  }

  if (!corr.data?.assets?.length || corr.data.assets.length < 2) {
    return (
      <EmptyState
        icon={Grid3x3}
        title="Add more holdings"
        body="A correlation matrix needs at least 2 assets with price history."
      />
    );
  }

  const assetCount = corr.data?.assets?.length ?? 0;
  const factorCount = Object.keys(factors.data?.exposures ?? {}).length;

  const lastPairCorr = rolling.data?.timeline?.length
    ? rolling.data.timeline[rolling.data.timeline.length - 1]?.value ?? null
    : null;

  const metrics: MetricItem[] = [
    { label: "Assets", value: String(assetCount), metricKey: "assets_count", metricValue: assetCount },
    { label: "Factors", value: String(factorCount), metricKey: "factors_count", metricValue: factorCount },
    {
      label: "Pair Correlation",
      value: formatNumber(lastPairCorr, { digits: 3 }),
      metricKey: "pair_correlation",
      metricValue: lastPairCorr,
    },
  ];

  return (
    <div className="space-y-0">
      <SummaryStrip metrics={metrics} />

      <div className="space-y-3 pt-4">
        <Tabs defaultValue="matrix">
          <TabsList>
            <TabsTrigger value="matrix">Matrix</TabsTrigger>
            <TabsTrigger value="factors">Factors</TabsTrigger>
          </TabsList>
          <TabsContent value="matrix" className="space-y-3">
            <section
              className="rounded-md p-3"
              style={{ background: "rgb(var(--c-surface))" }}
            >
              <HeatmapChart
                ariaLabel="Portfolio correlation matrix"
                className="h-96"
                rows={corr.data?.assets ?? []}
                cols={corr.data?.assets ?? []}
                values={corr.data?.matrix ?? []}
                min={-1}
                max={1}
              />
            </section>
            {pair && (
              <section
                className="rounded-md p-3"
                style={{ background: "rgb(var(--c-surface))" }}
              >
                <div className="mb-2 flex flex-wrap gap-2 text-xs">
                  {(["a", "b"] as const).map((side, index) => (
                    <select
                      key={side}
                      value={pair[index]}
                      onChange={(event) =>
                        setPair((old) =>
                          old
                            ? index
                              ? [old[0], event.target.value]
                              : [event.target.value, old[1]]
                            : old
                        )
                      }
                      className="rounded border border-line bg-surface px-2 py-1"
                    >
                      {(corr.data?.assets ?? []).map((asset: string) => (
                        <option key={asset}>{asset}</option>
                      ))}
                    </select>
                  ))}
                </div>
                <RollingBetaChart
                  name="Rolling correlation"
                  yName="Correlation"
                  timeline={rolling.data?.timeline ?? []}
                />
              </section>
            )}
          </TabsContent>
          <TabsContent value="factors">
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
              {Object.entries(factors.data?.exposures ?? {}).map(
                ([factor, value]) => (
                  <div
                    key={factor}
                    className="rounded-md p-3"
                    style={{ background: "rgb(var(--c-surface))" }}
                  >
                    <div className="flex items-center gap-1 text-[11px] uppercase text-text-muted">
                      {factor}
                      <MetricTooltip metricKey="factor_exposure" value={Number(value)} />
                    </div>
                    <b
                      style={{
                        fontFamily: '"JetBrains Mono", monospace',
                        fontVariantNumeric: "tabular-nums",
                      }}
                    >
                      {formatNumber(Number(value), { digits: 3 })}
                    </b>
                  </div>
                )
              )}
            </div>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
