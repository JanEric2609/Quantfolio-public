import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";

export interface GaugeChartProps extends Omit<EChartProps, "option"> {
  value: number;
  min?: number;
  max?: number;
  label?: string;
}

export function GaugeChart({ value, min = 0, max = 100, label = "Score", ...rest }: GaugeChartProps) {
  const option: EChartsOption = {
    series: [{
      type: "gauge" as const,
      min,
      max,
      progress: { show: true, width: 12 },
      axisLine: { lineStyle: { width: 12, color: [[1, "rgb(var(--c-surface-2))"]] } },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { show: false },
      pointer: { show: false },
      title: { show: false },
      detail: { valueAnimation: true, fontSize: 24, fontWeight: "bold", color: "rgb(var(--c-text-primary))", formatter: "{value}" },
      data: [{ value, name: label }],
    }],
  };
  return <EChart option={option} {...rest} />;
}
