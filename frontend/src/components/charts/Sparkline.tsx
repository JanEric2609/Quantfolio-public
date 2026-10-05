import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";

export interface SparklineProps extends Omit<EChartProps, "option" | "height"> {
  data: number[];
  tone?: "neutral" | "up" | "down";
}

export function Sparkline({ data, tone = "neutral", ...rest }: SparklineProps) {
  const color = tone === "up" ? "#22c55e" : tone === "down" ? "#ef4444" : "rgb(var(--c-text-muted))";
  const option: EChartsOption = {
    grid: { left: 0, right: 0, top: 4, bottom: 4 },
    xAxis: { type: "category" as const, show: false, data: data.map((_, i) => i) },
    yAxis: { type: "value" as const, show: false },
    series: [{ type: "line" as const, data, smooth: true, symbol: "none", lineStyle: { color, width: 1.5 } }],
  };
  return <EChart option={option} height={32} {...rest} />;
}
