import type { EChartsOption } from "echarts";
import * as echarts from "echarts/core";
const graphic = echarts.graphic;
import { EChart, type EChartProps } from "./EChart";

export interface LineChartProps extends Omit<EChartProps, "option"> {
  series: { name: string; data: [number | string, number][]; color?: string }[];
  xType?: "time" | "category";
  yFormat?: (v: number) => string;
  tooltipFormat?: (v: number) => string;
  area?: boolean;
  xName?: string;
  yName?: string;
  /** Logarithmic value axis (for growth indices); values must be positive. */
  yLog?: boolean;
  /** Bezier smoothing invents values between points; off for data series. */
  smooth?: boolean;
}

function isFinitePoint(point: [number | string, number]): boolean {
  return Number.isFinite(point[1]);
}

function areaGradient(color: string) {
  return new graphic.LinearGradient(0, 0, 0, 1, [
    { offset: 0, color: color + "33" },
    { offset: 1, color: color + "00" },
  ]);
}

export function LineChart({
  series, xType = "time", yFormat, tooltipFormat, area = false, xName, yName, yLog = false, smooth = false, ...rest
}: LineChartProps) {
  const option: EChartsOption = {
    tooltip: {
      trigger: "axis" as const,
      axisPointer: { type: "cross", crossStyle: { opacity: 0.2 } },
      valueFormatter: (tooltipFormat ?? yFormat ?? undefined)
        ? (value: unknown) => (tooltipFormat ?? yFormat)!(value as number)
        : undefined,
    },
    legend: series.length > 1 ? { top: 0, textStyle: { fontSize: 11 } } : undefined,
    grid: { left: yName ? 28 : 12, right: 16, top: 32, bottom: xName ? 28 : 8, containLabel: true },
    xAxis: {
      type: xType === "time" ? "time" : "category",
      splitLine: { show: false },
      name: xName,
      nameLocation: "middle",
      nameGap: 26,
    },
    yAxis: {
      type: yLog ? "log" : "value",
      scale: !yLog,
      name: yName,
      nameLocation: "middle",
      nameGap: 44,
      axisLabel: { formatter: yFormat ?? undefined },
      splitLine: { lineStyle: { opacity: 0.5 } },
    },
    series: series.map((s) => ({
      name: s.name,
      type: "line",
      data: s.data.filter(isFinitePoint).filter((p) => !yLog || p[1] > 0),
      smooth,
      ...(s.color ? { itemStyle: { color: s.color }, lineStyle: { width: 2, color: s.color } } : { lineStyle: { width: 2 } }),
      symbol: "none",
      ...(area && {
        areaStyle: s.color
          ? { color: areaGradient(s.color), opacity: 1 }
          : { opacity: 0.12 },
      }),
    })),
  };
  return <EChart option={option} {...rest} />;
}
