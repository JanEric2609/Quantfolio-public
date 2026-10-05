import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";

export interface AreaChartProps extends Omit<EChartProps, "option"> {
  series: { name: string; data: [number | string, number][] }[];
  xType?: "time" | "category";
  yFormat?: (v: number) => string;
  tone?: "neutral" | "danger";
}

export function AreaChart({ series, xType = "time", yFormat, tone = "neutral", ...rest }: AreaChartProps) {
  const areaStyle = tone === "danger" ? { color: "rgba(239, 68, 68, 0.3)" } : {};
  const option: EChartsOption = {
    tooltip: { trigger: "axis" as const },
    grid: { left: 48, right: 24, top: 32, bottom: 40 },
    xAxis: {
      type: xType === "time" ? "time" : "category",
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      axisLabel: { formatter: yFormat ?? undefined },
    },
    series: series.map((s) => ({
      name: s.name,
      type: "line",
      data: s.data,
      smooth: false,
      symbol: "none",
      lineStyle: { width: 2 },
      areaStyle: areaStyle,
    })),
  };
  return <EChart option={option} {...rest} />;
}
