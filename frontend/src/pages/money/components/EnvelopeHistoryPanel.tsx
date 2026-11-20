import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import { Card, CardContent } from "../../../components/ui/card";

interface EnvelopeHistoryRow {
  category_id: string;
  category_name: string;
  color: string;
  months_over_budget: number;
  months_with_budget: number;
  consistently_over: boolean;
  consistently_under: boolean;
}

export function EnvelopeHistoryPanel() {
  const history = useQuery({
    queryKey: ["envelope-history"],
    queryFn: () => api<EnvelopeHistoryRow[]>("/api/budget/envelopes/history?months=6"),
  });

  const rows = (history.data ?? []).filter((row) => row.months_with_budget > 0);
  const flagged = rows.filter((row) => row.consistently_over || row.consistently_under);

  if (history.isLoading) return <p className="text-sm text-text-muted">Loading envelope history…</p>;
  if (flagged.length === 0) return null;

  return (
    <Card>
      <CardContent className="pt-6 space-y-3">
        <h3 className="text-sm font-medium text-text-secondary">Envelope over/under history (last 6 months)</h3>
        <ul className="space-y-2">
          {flagged.map((row) => (
            <li key={row.category_id} className="flex items-center justify-between gap-2 text-sm">
              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: row.color }} />
                <span>{row.category_name}</span>
              </div>
              <span className={row.consistently_over ? "text-danger" : "text-success"}>
                {row.consistently_over
                  ? `Over budget ${row.months_over_budget}/${row.months_with_budget} months`
                  : "Consistently under budget"}
              </span>
            </li>
          ))}
        </ul>
        <p className="text-xs text-text-muted">Consider adjusting these envelopes' monthly targets.</p>
      </CardContent>
    </Card>
  );
}
