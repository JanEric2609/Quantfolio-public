import { BarChart } from "../../../components/charts/BarChart";

/**
 * Daily returns binned by the Freedman-Diaconis rule (bin width 2·IQR/n^(1/3)),
 * so the bins fit the data's own spread instead of fixed 1 % steps that put a
 * diversified book into two or three bars.
 */
export function histogramBins(values: number[], maxBins = 40): { label: string; count: number }[] {
  const xs = values.filter(Number.isFinite).slice().sort((a, b) => a - b);
  const n = xs.length;
  if (n < 2) return [];
  const q = (p: number) => xs[Math.min(n - 1, Math.max(0, Math.floor(p * (n - 1))))];
  const iqr = q(0.75) - q(0.25);
  const lo = xs[0];
  const hi = xs[n - 1];
  if (hi === lo) return [{ label: `${(lo * 100).toFixed(2)}`, count: n }];
  let width = iqr > 0 ? (2 * iqr) / Math.cbrt(n) : (hi - lo) / 10;
  let k = Math.ceil((hi - lo) / width);
  if (k > maxBins) {
    k = maxBins;
    width = (hi - lo) / k;
  }
  const counts = new Array(k).fill(0);
  for (const x of xs) counts[Math.min(k - 1, Math.floor((x - lo) / width))] += 1;
  return counts.map((count, i) => ({ label: ((lo + (i + 0.5) * width) * 100).toFixed(2), count }));
}

export function ReturnHistogram({ values = [] }: { values?: number[] }) {
  const bins = histogramBins(values);
  return (
    <BarChart
      ariaLabel="Distribution of daily portfolio returns"
      className="h-72"
      categories={bins.map((b) => b.label)}
      series={[{ name: "Trading days", data: bins.map((b) => b.count) }]}
      xName="Daily return, bin centre (%)"
      yName="Trading days"
    />
  );
}
