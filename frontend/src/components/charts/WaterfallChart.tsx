import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";

/** A step (``delta`` added to the running total) or, with ``total``, a bar of the running total itself. */
export interface WaterfallItem { label: string; delta: number; total?: boolean; }

export interface WaterfallChartProps extends Omit<EChartProps, "option"> {
  items: WaterfallItem[];
  yName?: string;
  format?: (v: number) => string;
}

/**
 * A real waterfall: each step floats from the running total before it, a
 * total bar stands on zero. The base series is invisible.
 */
export function WaterfallChart({ items, yName, format, ...rest }: WaterfallChartProps) {
  const fmt = format ?? ((v: number) => v.toLocaleString("de-DE", { maximumFractionDigits: 2 }));
  let running = 0;
  const base: number[] = [];
  const bars: { value: number; itemStyle: { color: string }; shown: number }[] = [];
  for (const item of items) {
    const delta = Number.isFinite(item.delta) ? item.delta : 0;
    if (item.total) {
      running = delta;
      base.push(Math.min(0, delta));
      bars.push({ value: Math.abs(delta), itemStyle: { color: "#60a5fa" }, shown: delta });
      continue;
    }
    const start = running;
    running += delta;
    base.push(Math.min(start, running));
    bars.push({ value: Math.abs(delta), itemStyle: { color: delta >= 0 ? "#22c55e" : "#ef4444" }, shown: delta });
  }
  const option: EChartsOption = {
    tooltip: {
      trigger: "axis" as const,
      axisPointer: { type: "shadow" as const },
      formatter: (p: unknown) => {
        const arr = p as Array<{ name: string; dataIndex: number; seriesIndex: number }>;
        const hit = arr.find((x) => x.seriesIndex === 1) ?? arr[0];
        return `${hit.name}: ${fmt(bars[hit.dataIndex]?.shown ?? 0)}`;
      },
    },
    grid: { left: 56, right: 24, top: 32, bottom: 56 },
    xAxis: { type: "category" as const, data: items.map((i) => i.label), axisLabel: { interval: 0, rotate: items.length > 6 ? 25 : 0 } },
    yAxis: { type: "value" as const, name: yName, nameLocation: "middle", nameGap: 44, axisLabel: { formatter: (v: number) => fmt(v) } },
    series: [
      { type: "bar" as const, stack: "w", data: base, itemStyle: { color: "transparent" }, emphasis: { disabled: true }, silent: true },
      { type: "bar" as const, stack: "w", data: bars.map(({ value, itemStyle }) => ({ value, itemStyle })) },
    ],
  };
  return <EChart option={option} {...rest} />;
}
