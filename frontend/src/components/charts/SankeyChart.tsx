import type { EChartsOption } from "echarts";
import { EChart, type EChartProps } from "./EChart";

export interface SankeyLink {
  source: string;
  target: string;
  value: number;
}

export interface SankeyChartProps extends Omit<EChartProps, "option"> {
  links: SankeyLink[];
  valueFormatter?: (value: number) => string;
}

/** Sankey nodes are derived from the link endpoints — every distinct source/target name becomes one node. */
export function sankeyNodesFromLinks(links: SankeyLink[]): { name: string }[] {
  const nodeNames = new Set<string>();
  links.forEach((link) => {
    nodeNames.add(link.source);
    nodeNames.add(link.target);
  });
  return Array.from(nodeNames, (name) => ({ name }));
}

export function SankeyChart({ links, valueFormatter, ...rest }: SankeyChartProps) {
  const option: EChartsOption = {
    tooltip: {
      trigger: "item" as const,
      triggerOn: "mousemove",
      formatter: (params: any) =>
        params.dataType === "edge"
          ? `${params.data.source} → ${params.data.target}: ${valueFormatter ? valueFormatter(params.data.value) : params.data.value}`
          : params.name,
    },
    series: [
      {
        type: "sankey",
        emphasis: { focus: "adjacency" },
        lineStyle: { color: "gradient", curveness: 0.5 },
        label: { fontSize: 11 },
        data: sankeyNodesFromLinks(links),
        links,
      },
    ],
  };
  return <EChart option={option} {...rest} />;
}
