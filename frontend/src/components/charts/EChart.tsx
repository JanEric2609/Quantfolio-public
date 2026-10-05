// Use the ESM build: the CJS build (lib/core) resolves to the module object
// under Vite 8's rolldown CJS interop, breaking <ReactEChartsCore /> (React #130).
import ReactEChartsCore from "echarts-for-react/esm/core";
import type { EChartsOption } from "echarts";
import { echarts } from "../../lib/echarts";
import "../../lib/theme/echarts-bloomberg";
import { Skeleton } from "../ui/skeleton";

export interface EChartProps {
  option: EChartsOption;
  loading?: boolean;
  error?: string | null;
  height?: number | string;
  ariaLabel: string;
  className?: string;
}

/**
 * A fixed pixel height is right on a desktop and far too tall in a 390 px wide
 * phone viewport (a 360 px chart fills the screen and pushes the content below
 * out of reach). Tall charts therefore shrink with the viewport width, but never
 * below 60 % of their nominal height and never above it, so anything wider than
 * ~480 px renders exactly as before. Sparklines and other short charts are untouched.
 */
export function responsiveChartHeight(height: number | string): string {
  if (typeof height !== "number") return height;
  if (height < 160) return `${height}px`;
  return `min(${height}px, max(${Math.round(height * 0.6)}px, 75vw))`;
}

export function EChart({ option, loading, error, height = 320, ariaLabel, className }: EChartProps) {
  const h = responsiveChartHeight(height);
  const isDark = typeof document !== "undefined" ? (document.documentElement.getAttribute("data-theme") ?? "dark") === "dark" : true;
  const theme = isDark ? "bloomberg" : "bloomberg-light";

  if (loading) return <Skeleton className={`w-full rounded-md ${className ?? ""}`} style={{ height: h }} />;
  if (error) return <div role="alert" className={`text-sm text-danger ${className ?? ""}`}>{error}</div>;
  return (
    <div role="img" aria-label={ariaLabel} className={className} style={{ height: h }}>
      <ReactEChartsCore
        echarts={echarts}
        option={option}
        theme={theme}
        style={{ height: "100%", width: "100%" }}
        notMerge
        lazyUpdate
      />
    </div>
  );
}
