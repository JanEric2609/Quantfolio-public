import { useOutletContext } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { BookOpenText } from "lucide-react";
import { Card } from "../../components/ui/card";
import { Badge } from "../../components/ui/badge";
import { Skeleton } from "../../components/ui/skeleton";
import { EmptyState } from "../../components/composed/EmptyState";
import { formatDateTime, formatPercent } from "../../lib/format";
import { api, type MandateJournalEntry } from "../../lib/api";
import type { MandatesOutletContext } from "./MandatesHub";

const STATUS_VARIANT: Record<string, "success" | "warning" | "danger" | "secondary"> = {
  completed: "success",
  winner: "success",
  llm_unavailable: "danger",
  // Distinct from fallback_hold: the model never produced parseable JSON
  // even under the grammar-constrained schema plus one correction retry,
  // rather than running out of tool-call turns while otherwise cooperating.
  review_failed: "danger",
  fallback_hold: "warning",
  invalid_action: "warning",
};

const ACTION_VARIANT: Record<string, "success" | "danger" | "secondary"> = {
  buy: "success",
  sell: "danger",
  hold: "secondary",
};

const THESIS_TRUNCATE = 220;

function truncate(text: string | null | undefined, max: number): string {
  if (!text) return "—";
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export function JournalTab() {
  const { portfolioId, mandate } = useOutletContext<MandatesOutletContext>();

  const journalQuery = useQuery({
    queryKey: ["llm-portfolio-journal", portfolioId],
    queryFn: () => api<MandateJournalEntry[]>(`/api/llm-portfolio/${portfolioId}/journal`),
    enabled: portfolioId != null,
  });

  if (portfolioId == null) {
    return (
      <EmptyState
        icon={BookOpenText}
        title={`No Mandate ${mandate} portfolio yet`}
        description="Create the mandate on the Overview tab before a journal can exist."
      />
    );
  }

  if (journalQuery.isLoading) {
    return <div className="space-y-2">{[...Array(6)].map((_, i) => <Skeleton key={i} className="h-14 w-full" />)}</div>;
  }

  const entries = journalQuery.data ?? [];

  if (entries.length === 0) {
    return (
      <EmptyState
        icon={BookOpenText}
        title="No decisions yet"
        description="Trigger a review from the Overview tab to record the first journal entry."
      />
    );
  }

  return (
    <Card className="p-0 overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-text-muted border-b border-border">
            <th className="py-2 pl-4 pr-3">Date</th>
            <th className="py-2 pr-3">Source</th>
            <th className="py-2 pr-3">Action</th>
            <th className="py-2 pr-3">Ticker</th>
            <th className="py-2 pr-3 text-right">Confidence</th>
            <th className="py-2 pr-3">Status</th>
            <th className="py-2 pr-4">Thesis</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => {
            const decision = entry.decision_json ?? {};
            const action = (decision.action ?? "—").toLowerCase();
            return (
              <tr key={`${entry.source}-${entry.id}`} className="border-b border-border/50 align-top">
                <td className="py-2 pl-4 pr-3 whitespace-nowrap">{formatDateTime(entry.review_date ?? entry.created_at)}</td>
                <td className="py-2 pr-3">
                  <Badge variant="secondary" className="text-[10px] capitalize">{entry.source}</Badge>
                </td>
                <td className="py-2 pr-3">
                  <Badge variant={ACTION_VARIANT[action] ?? "secondary"} className="text-[10px] uppercase">
                    {action}
                  </Badge>
                </td>
                <td className="py-2 pr-3 font-mono">{decision.ticker || "—"}</td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  {decision.confidence != null ? formatPercent(decision.confidence) : "—"}
                </td>
                <td className="py-2 pr-3">
                  <Badge variant={STATUS_VARIANT[entry.status] ?? "secondary"} className="text-[10px]">
                    {entry.status}
                    {entry.verdict ? ` · ${entry.verdict}` : ""}
                  </Badge>
                </td>
                <td className="py-2 pr-4 text-xs text-text-secondary max-w-md">
                  {truncate(decision.thesis, THESIS_TRUNCATE)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Card>
  );
}
