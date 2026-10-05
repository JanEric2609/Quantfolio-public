import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { getResearchExtracts, type ResearchExtract } from "../../../lib/api";
import { Card, CardContent } from "../../../components/ui/card";

function ageLabel(extract: ResearchExtract): string {
  if (!extract.newest) return "Not loaded";
  const age = extract.age_days ?? 0;
  return `Newest ${extract.newest} (${age} day${age === 1 ? "" : "s"} old)`;
}

export function ResearchExtractsPanel() {
  const extracts = useQuery({
    queryKey: ["research-extracts"],
    queryFn: getResearchExtracts,
    retry: 1,
  });

  const rows = extracts.data ?? [];
  const staleCount = rows.filter((e) => e.stale).length;

  return (
    <Card className="bg-surface border-border">
      <CardContent className="pt-4">
        {extracts.isLoading && <div className="text-sm text-text-muted">Loading…</div>}
        {extracts.isError && (
          <div className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
            Failed to load research extracts
          </div>
        )}
        {staleCount > 0 && (
          <div className="mb-3 rounded-md border border-warn/40 bg-warn/10 p-3 text-sm text-warn">
            {staleCount} extract{staleCount === 1 ? " is" : "s are"} out of date.
          </div>
        )}
        <div className="grid gap-3 md:grid-cols-2">
          {rows.map((e) => (
            <div key={e.key} className="rounded-md border border-border bg-surface-2 p-4" data-testid={`extract-${e.key}`}>
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-medium">{e.label}</span>
                {e.stale ? (
                  <AlertTriangle className="h-4 w-4 text-warn" aria-label="stale" />
                ) : (
                  <CheckCircle2 className="h-4 w-4 text-success" aria-label="current" />
                )}
              </div>
              <div className={`mt-1 text-xs ${e.stale ? "text-warn" : "text-text-secondary"}`}>{ageLabel(e)}</div>
              <div className="mt-1 text-xs text-text-muted">
                {e.rows.toLocaleString()} rows · stale after {e.stale_after_days} days
              </div>
              <div className="mt-2 text-xs text-text-secondary">{e.used_for}</div>
              <div className="mt-1 text-[11px] text-text-muted">{e.refresh}</div>
              {e.error && <div className="mt-1 text-[11px] text-danger">{e.error}</div>}
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
