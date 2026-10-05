import { ScatterChart } from "../../../components/charts/ScatterChart";

const pct = (v: number) => `${(v * 100).toFixed(1)} %`;

/** Least-squares line through the plotted [benchmark, portfolio] daily returns. */
export function fitLine(points: [number, number][]): { slope: number; intercept: number } | undefined {
  const n = points.length;
  if (n < 3) return undefined;
  const mx = points.reduce((s, p) => s + p[0], 0) / n;
  const my = points.reduce((s, p) => s + p[1], 0) / n;
  let sxy = 0;
  let sxx = 0;
  for (const [x, y] of points) {
    sxy += (x - mx) * (y - my);
    sxx += (x - mx) ** 2;
  }
  if (sxx === 0) return undefined;
  const slope = sxy / sxx;
  return { slope, intercept: my - slope * mx };
}

export function RegressionScatter({ data = [], benchmark }: { data?: [number, number][]; benchmark?: string }) {
  const line = fitLine(data);
  return (
    <ScatterChart
      ariaLabel="Daily returns of the portfolio against the benchmark"
      className="h-72"
      series={[{ name: "Trading days", data }]}
      regression={line ? { ...line, name: `Fit: slope ${line.slope.toFixed(2)} (beta)` } : undefined}
      xName={`${benchmark ?? "Benchmark"} daily return (EUR)`}
      yName="Portfolio daily return (EUR)"
      xFormat={pct}
      yFormat={pct}
    />
  );
}
