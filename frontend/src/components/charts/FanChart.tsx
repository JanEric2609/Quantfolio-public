import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";

export interface FanBand { p05: number; p25: number; median: number; p75: number; p95: number; }

export interface FanChartProps extends Omit<EChartProps, "option"> {
  steps: number;
  bands: FanBand[];
  /** x labels, one per band (defaults to 0..steps-1). */
  labels?: (string | number)[];
  xName?: string;
  yName?: string;
  yFormat?: (v: number) => string;
  /** Extra lines drawn on top, e.g. money put in or a goal. */
  lines?: { name: string; data: number[]; color?: string; dashed?: boolean }[];
}

/**
 * Percentile fan: the 5-95 and 25-75 ranges as shaded bands (stacked on an
 * invisible base, so each band spans its two percentiles), the median as a line.
 */
export function FanChart({ steps, bands, labels, xName, yName, yFormat, lines = [], ...rest }: FanChartProps) {
  const xData = labels ?? Array.from({ length: steps }, (_, i) => i);
  const stack = (name: string, data: number[], color: string, opacity: number, stackId: string) => ({
    name, type: "line" as const, data, stack: stackId, symbol: "none", lineStyle: { width: 0 },
    areaStyle: { color, opacity }, emphasis: { disabled: true },
  });
  const option: EChartsOption = {
    tooltip: {
      trigger: "axis" as const,
      formatter: (p: unknown) => {
        const i = (p as Array<{ dataIndex: number }>)[0]?.dataIndex ?? 0;
        const b = bands[i];
        if (!b) return "";
        const f = yFormat ?? ((v: number) => v.toFixed(0));
        const extra = lines.map((l) => `<br/>${l.name}: ${f(l.data[i])}`).join("");
        return `${xName ?? ""} ${xData[i]}<br/>95th: ${f(b.p95)}<br/>75th: ${f(b.p75)}<br/><b>Median: ${f(b.median)}</b>`
          + `<br/>25th: ${f(b.p25)}<br/>5th: ${f(b.p05)}${extra}`;
      },
    },
    legend: { data: ["5-95 %", "25-75 %", "Median", ...lines.map((l) => l.name)], top: 0 },
    grid: { left: 64, right: 24, top: 36, bottom: xName ? 48 : 32 },
    xAxis: { type: "category" as const, data: xData, name: xName, nameLocation: "middle", nameGap: 28 },
    yAxis: { type: "value" as const, name: yName, nameLocation: "middle", nameGap: 52, axisLabel: { formatter: yFormat } },
    series: [
      stack("base", bands.map((b) => b.p05), "transparent", 0, "outer"),
      stack("5-95 %", bands.map((b) => b.p95 - b.p05), "#60a5fa", 0.18, "outer"),
      stack("base2", bands.map((b) => b.p25), "transparent", 0, "inner"),
      stack("25-75 %", bands.map((b) => b.p75 - b.p25), "#3b82f6", 0.3, "inner"),
      { name: "Median", type: "line" as const, data: bands.map((b) => b.median), symbol: "none", lineStyle: { width: 2.5, color: "#1d4ed8" } },
      ...lines.map((l) => ({
        name: l.name, type: "line" as const, data: l.data, symbol: "none",
        lineStyle: { width: 1.5, color: l.color ?? "#94a3b8", type: l.dashed ? ("dashed" as const) : ("solid" as const) },
      })),
    ] as EChartsOption["series"],
  };
  return <EChart option={option} {...rest} />;
}
