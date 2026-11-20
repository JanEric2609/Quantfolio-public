import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";

export interface ScatterPoint { name: string; data: [number, number][]; symbol?: string; }

export interface ScatterChartProps extends Omit<EChartProps, "option"> {
  series: ScatterPoint[];
  /** A line y = slope·x + intercept in the units of the plotted points. */
  regression?: { slope: number; intercept: number; name?: string };
  /** x span of the line; defaults to the span of the plotted points. */
  regressionRange?: [number, number];
  xName?: string;
  yName?: string;
  xFormat?: (v: number) => string;
  yFormat?: (v: number) => string;
}

interface SeriesEntry {
  name: string;
  type: string;
  data: number[][];
  symbolSize?: number;
  symbol?: string;
  lineStyle?: { type: string; color: string };
}

function span(series: ScatterPoint[]): [number, number] | null {
  const xs = series.flatMap((s) => s.data.map((p) => p[0])).filter(Number.isFinite);
  if (xs.length === 0) return null;
  return [Math.min(...xs), Math.max(...xs)];
}

export function ScatterChart({
  series, regression, regressionRange, xName, yName, xFormat, yFormat, ...rest
}: ScatterChartProps) {
  const scatterSeries: SeriesEntry[] = series.map((s) => ({ name: s.name, type: "scatter", data: s.data, symbolSize: 6, symbol: s.symbol ?? "circle" }));
  const range = regressionRange ?? span(series);
  if (regression && range) {
    // Drawn only across the data: a line out to x = 100 used to stretch both
    // axes a hundredfold and squash daily returns into a dot at the origin.
    const [x0, x1] = range;
    scatterSeries.push({
      name: regression.name ?? "Regression",
      type: "line",
      data: [[x0, regression.slope * x0 + regression.intercept], [x1, regression.slope * x1 + regression.intercept]],
      lineStyle: { type: "dashed", color: "#a78bfa" },
      symbol: "none",
    });
  }
  const option: EChartsOption = {
    tooltip: {
      trigger: "item" as const,
      formatter: (p: unknown) => {
        const v = (p as { value?: number[] }).value ?? [];
        const fx = xFormat ?? ((n: number) => String(n));
        const fy = yFormat ?? ((n: number) => String(n));
        return `${xName ?? "x"}: ${fx(v[0])}<br/>${yName ?? "y"}: ${fy(v[1])}`;
      },
    },
    grid: { left: 56, right: 24, top: 32, bottom: 48 },
    xAxis: {
      type: "value" as const, name: xName, nameLocation: "middle", nameGap: 28, scale: true,
      axisLabel: xFormat ? { formatter: xFormat } : undefined,
    },
    yAxis: {
      type: "value" as const, name: yName, nameLocation: "middle", nameGap: 44, scale: true,
      axisLabel: yFormat ? { formatter: yFormat } : undefined,
    },
    series: scatterSeries as unknown as EChartsOption["series"],
  };
  return <EChart option={option} {...rest} />;
}
