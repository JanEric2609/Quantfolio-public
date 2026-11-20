import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";

export interface BarChartProps extends Omit<EChartProps, "option"> {
  categories: string[];
  series: { name: string; data: number[] }[];
  horizontal?: boolean;
  xName?: string;
  yName?: string;
  yFormat?: (v: number) => string;
}

export function BarChart({ categories, series, horizontal = false, xName, yName, yFormat, ...rest }: BarChartProps) {
  const named = (axis: object, name?: string, gap = 28) =>
    name ? { ...axis, name, nameLocation: "middle" as const, nameGap: gap } : axis;
  const option: EChartsOption = {
    tooltip: { trigger: "axis" as const },
    grid: { left: yName ? 56 : 48, right: 24, top: 32, bottom: xName ? 48 : 40 },
    xAxis: named(
      horizontal
        ? { type: "value" as const }
        : { type: "category" as const, data: categories, splitLine: { show: false } },
      xName,
    ),
    yAxis: named(
      horizontal
        ? { type: "category" as const, data: categories }
        : { type: "value" as const, axisLabel: yFormat ? { formatter: yFormat } : undefined },
      yName,
      44,
    ),
    series: series.map((s) => ({
      name: s.name,
      type: "bar",
      data: s.data,
      barMaxWidth: 40,
    })),
  };
  return <EChart option={option} {...rest} />;
}
