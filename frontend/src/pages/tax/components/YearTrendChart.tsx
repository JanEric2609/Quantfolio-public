import { useQueries } from "@tanstack/react-query";
import { LineChart } from "../../../components/charts/LineChart";
import { api, type TaxOverview } from "../../../lib/api";

const CURRENT_YEAR = new Date().getFullYear();

export function YearTrendChart({ year }: { year: number }) {
  const years = [year - 4, year - 3, year - 2, year - 1, year];

  const results = useQueries({
    queries: years.map((y) => ({
      queryKey: ["tax", "overview", y],
      queryFn: () => api<TaxOverview>(`/api/tax/overview?year=${y}`),
      enabled: true,
    })),
  });

  const data: [string, number][] = [];
  for (let i = 0; i < years.length; i++) {
    const d = results[i].data;
    if (d) {
      data.push([String(years[i]), d.tax.total_tax_due_eur as number]);
    }
  }

  if (data.length === 0) return null;

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium text-text-secondary">Tax trend (5 years)</h3>
      <LineChart
        series={[{ name: "Total tax (EUR)", data }]}
        xType="category"
        ariaLabel="Year-over-year tax estimate trend"
        height={280}
      />
    </div>
  );
}
