import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";

export interface DonutSlice { name: string; value: number; }

export interface DonutChartProps extends Omit<EChartProps, "option"> {
  slices: DonutSlice[];
  innerRadius?: number;
  /** Largest slices shown on their own; the rest become one "Other (n)" slice. */
  maxSlices?: number;
}

/** The largest ``maxSlices`` slices, the rest summed into "Other (n)". */
export function groupSlices(slices: DonutSlice[], maxSlices: number): DonutSlice[] {
  const sorted = slices.filter((s) => Number.isFinite(s.value) && s.value > 0).sort((a, b) => b.value - a.value);
  if (sorted.length <= maxSlices) return sorted;
  const head = sorted.slice(0, maxSlices - 1);
  const tail = sorted.slice(maxSlices - 1);
  return [...head, { name: `Other (${tail.length})`, value: tail.reduce((sum, s) => sum + s.value, 0) }];
}

export function DonutChart({ slices, innerRadius = 50, maxSlices = 8, ...rest }: DonutChartProps) {
  const data = groupSlices(slices, maxSlices);
  const total = data.reduce((sum, s) => sum + s.value, 0) || 1;
  const pct = (v: number) => `${((v / total) * 100).toFixed(v / total < 0.01 ? 1 : 0)} %`;
  const byName = new Map(data.map((s) => [s.name, s.value]));
  const option: EChartsOption = {
    tooltip: { trigger: "item" as const, formatter: (p: unknown) => {
      const { name, value } = p as { name: string; value: number };
      return `${name}: ${pct(value)}`;
    } },
    legend: {
      type: "scroll" as const,
      orient: "vertical" as const,
      right: 0,
      top: "middle",
      width: "45%",
      formatter: (name: string) => `${name}  ${pct(byName.get(name) ?? 0)}`,
    },
    series: [{
      type: "pie" as const,
      center: ["28%", "50%"],
      radius: [`${innerRadius}%`, "75%"],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 4 },
      label: { show: false },
      emphasis: { label: { show: false } },
      data: data.map((s) => ({ name: s.name, value: s.value })),
    }],
  };
  return <EChart option={option} {...rest} />;
}
