import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { api } from "../../../lib/api";
import { formatCurrency } from "../../../lib/format";
import { SankeyChart } from "../../../components/charts/SankeyChart";
import { Button } from "../../../components/ui/button";

interface SankeyResponse {
  year: number;
  total_income: number;
  total_expense: number;
  links: { source: string; target: string; value: number }[];
}

export function AnnualSankeyChart() {
  const [year, setYear] = useState(() => new Date().getFullYear());

  const sankey = useQuery({
    queryKey: ["budget-sankey", year],
    queryFn: () => api<SankeyResponse>(`/api/budget/reports/sankey?year=${year}`),
  });

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-text-secondary">Annual cash flow</h3>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            className="h-11 w-11 p-0 sm:h-7 sm:w-7"
            aria-label="Previous year"
            onClick={() => setYear((y) => y - 1)}
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <span className="w-12 text-center text-sm tabular-nums">{year}</span>
          <Button
            variant="ghost"
            size="sm"
            className="h-11 w-11 p-0 sm:h-7 sm:w-7"
            aria-label="Next year"
            disabled={year >= new Date().getFullYear()}
            onClick={() => setYear((y) => y + 1)}
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>
      {!sankey.isLoading && sankey.data && (sankey.data.links ?? []).length === 0 && (
        <p className="text-sm text-text-muted">No transactions recorded for {year} yet.</p>
      )}
      {(sankey.isLoading || (sankey.data?.links?.length ?? 0) > 0) && (
        <SankeyChart
          links={sankey.data?.links ?? []}
          valueFormatter={(v) => formatCurrency(v)}
          loading={sankey.isLoading}
          error={sankey.error ? "Failed to load annual cash flow" : null}
          height={420}
          ariaLabel={`Annual income and spending flow for ${year}`}
        />
      )}
    </div>
  );
}
