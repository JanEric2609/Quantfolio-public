import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";

export interface DonutSlice { name: string; value: number; }

export interface DonutChartProps extends Omit<EChartProps, "option"> {
  slices: DonutSlice[];
  innerRadius?: number;
}

export function DonutChart({ slices, innerRadius = 40, ...rest }: DonutChartProps) {
  const option: EChartsOption = {
    tooltip: { trigger: "item" as const, formatter: "{b}: {c} ({d}%)" },
    legend: { orient: "vertical" as const, right: 10, top: "center" },
    series: [{
      type: "pie" as const,
      radius: ["50%", "70%"],
      avoidLabelOverlap: false,
      itemStyle: { borderRadius: 4 },
      label: { show: false },
      emphasis: { label: { show: true, fontWeight: "bold" } },
      data: slices.map((s) => ({ name: s.name, value: s.value })),
    }],
  };
  return <EChart option={option} {...rest} />;
}
