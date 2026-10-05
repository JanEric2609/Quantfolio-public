import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";
import { formatNumber } from "../../lib/format";

export interface HeatmapChartProps extends Omit<EChartProps, "option"> {
  rows: string[];
  cols: string[];
  /** ``null`` = not computable (shown blank, never as 0). */
  values: (number | null)[][];
  min?: number;
  max?: number;
}

/** Diverging blue - white - red, so 0 reads as neutral and the sign as colour. */
const DIVERGING = ["#2166ac", "#67a9cf", "#d1e5f0", "#f7f7f7", "#fddbc7", "#ef8a62", "#b2182b"];

export function HeatmapChart({ rows, cols, values, min, max, ...rest }: HeatmapChartProps) {
  const data: [number, number, number | string][] = values.flatMap((row, i) =>
    row.map((v, j): [number, number, number | string] => [j, i, v == null ? "-" : v])
  );
  const finite = values.flat().filter((v): v is number => v != null && Number.isFinite(v));
  const mn = min ?? (finite.length ? Math.min(...finite) : 0);
  const mx = max ?? (finite.length ? Math.max(...finite) : 1);
  const option: EChartsOption = {
    tooltip: {
      formatter: (p: any) => {
        const v = p.data[2];
        const shown = typeof v === "number" ? formatNumber(v, { digits: 3 }) : "too few shared days";
        return `${rows[p.data[1]]} × ${cols[p.data[0]]}: <b>${shown}</b>`;
      },
    },
    grid: { left: 64, right: 80, top: 32, bottom: 48 },
    xAxis: { type: "category" as const, data: cols, splitArea: { show: false }, axisLabel: { rotate: cols.length > 6 ? 30 : 0 } },
    yAxis: { type: "category" as const, data: rows, splitArea: { show: false } },
    visualMap: { min: mn, max: mx, calculable: true, orient: "vertical" as const, right: 0, top: "middle", inRange: { color: DIVERGING } },
    series: [{
      type: "heatmap" as const, data,
      label: { show: rows.length <= 8, formatter: (p: any) => (typeof p.data[2] === "number" ? p.data[2].toFixed(2) : "") },
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: "rgba(0,0,0,0.5)" } },
    }],
  };
  return <EChart option={option} {...rest} />;
}
