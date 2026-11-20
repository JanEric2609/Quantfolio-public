import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";
import { BookOpenText } from "lucide-react";
import { Card } from "../../components/ui/card";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import { EmptyState } from "../../components/composed/EmptyState";
import { formatDateTime, formatPercent } from "../../lib/format";
import { api, type MandateDecision, type MandateJournalEntry } from "../../lib/api";
import { MANDATES, portfolioOf, type MandateLetter, type MandatesOutletContext } from "./mandateContext";
import { Table } from "../../components/ui/table";

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

/** The schema puts confidence inside ``expectation``; older rows had it on top. */
function confidenceOf(decision: MandateDecision): number | null {
  if (typeof decision.confidence === "number") return decision.confidence;
  const c = decision.expectation?.confidence;
  return typeof c === "number" ? c : null;
}

function truncate(text: string | null | undefined, max: number): string {
  if (!text) return "—";
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

type Filter = "all" | MandateLetter;

export function JournalTab() {
  const { portfolios, portfoliosLoading } = useOutletContext<MandatesOutletContext>();
  const [filter, setFilter] = useState<Filter>("all");
  const cols = MANDATES.map((m) => ({ mandate: m, portfolio: portfolioOf(portfolios, m) }));
  const results = useQueries({
    queries: cols.map(({ portfolio }) => ({
      queryKey: ["llm-portfolio-journal", portfolio?.id ?? null],
      queryFn: () => api<MandateJournalEntry[]>(`/api/llm-portfolio/${portfolio!.id}/journal`),
      enabled: portfolio != null,
    })),
  });

  if (portfoliosLoading || results.some((r) => r.isLoading)) {
    return <div className="space-y-2">{[...Array(6)].map((_, i) => <Skeleton key={i} className="h-14 w-full" />)}</div>;
  }

  // One timeline, newest first, each row marked with its mandate.
  const all = results
    .flatMap((r, i) => (r.data ?? []).map((entry) => ({ entry, mandate: cols[i].mandate })))
    .sort((a, b) =>
      String(b.entry.review_date ?? b.entry.created_at).localeCompare(String(a.entry.review_date ?? a.entry.created_at)),
    );
  const entries = filter === "all" ? all : all.filter((e) => e.mandate === filter);

  if (all.length === 0) {
    return (
      <EmptyState
        icon={BookOpenText}
        title="No decisions yet"
        description="Trigger a review from the Overview tab to record the first journal entry."
      />
    );
  }

  return (
    <div className="space-y-3">
      <div role="group" aria-label="Show mandate" className="flex w-fit items-center gap-1 rounded-md border border-border p-0.5">
        {(["all", ...MANDATES] as Filter[]).map((f) => (
          <Button
            key={f}
            type="button"
            size="sm"
            variant={filter === f ? "default" : "ghost"}
            aria-pressed={filter === f}
            onClick={() => setFilter(f)}
          >
            {f === "all" ? `Both (${all.length})` : `Mandate ${f} (${all.filter((e) => e.mandate === f).length})`}
          </Button>
        ))}
      </div>
      <JournalTable entries={entries} />
    </div>
  );
}

function JournalTable({ entries }: { entries: { entry: MandateJournalEntry; mandate: MandateLetter }[] }) {
  return (
    <Card className="p-0 overflow-x-auto">
      <Table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-text-muted border-b border-border">
            <th className="py-2 pl-4 pr-3">Date</th>
            <th className="py-2 pr-3">Mandate</th>
            <th className="py-2 pr-3">Source</th>
            <th className="py-2 pr-3">Action</th>
            <th className="py-2 pr-3">Ticker</th>
            <th className="py-2 pr-3 text-right">Confidence</th>
            <th className="py-2 pr-3">Status</th>
            <th className="py-2 pr-4">Thesis</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(({ entry, mandate }) => {
            const decision = entry.decision_json ?? {};
            const action = (decision.action ?? "—").toLowerCase();
            return (
              <tr key={`${mandate}-${entry.source}-${entry.id}`} className="border-b border-border/50 align-top">
                <td className="py-2 pl-4 pr-3 whitespace-nowrap">{formatDateTime(entry.review_date ?? entry.created_at)}</td>
                <td className="py-2 pr-3 font-semibold">
                  {mandate}
                  {entry.earlier_run ? (
                    <Badge variant="secondary" className="ml-1 text-[10px] font-normal" title="Made before the last paper reset">
                      earlier run
                    </Badge>
                  ) : null}
                </td>
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
                  {confidenceOf(decision) != null ? formatPercent(confidenceOf(decision)!) : "—"}
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
      </Table>
    </Card>
  );
}
