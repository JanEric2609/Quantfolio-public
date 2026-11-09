import { useOutletContext } from "react-router-dom";
import { useQueries } from "@tanstack/react-query";
import { Target } from "lucide-react";
import { Card } from "../../components/ui/card";
import { Skeleton } from "../../components/ui/skeleton";
import { EmptyState } from "../../components/composed/EmptyState";
import { hitRateSample, TOO_EARLY } from "../../lib/sampleSize";
import { formatPercent } from "../../lib/format";
import { api, type MandateAccuracy } from "../../lib/api";
import { MANDATES, portfolioOf, type MandatesOutletContext } from "./mandateContext";

type Row = { label: string; value: (a: MandateAccuracy) => string; tone?: (a: MandateAccuracy) => string };

const ROWS: Row[] = [
  {
    label: "Hit rate",
    value: (a) => (!hitRateSample(a.resolved).enough ? TOO_EARLY : a.hit_rate != null ? formatPercent(a.hit_rate) : "—"),
    tone: (a) =>
      !hitRateSample(a.resolved).enough || a.hit_rate == null ? "" : a.hit_rate >= 0.5 ? "text-success" : "text-danger",
  },
  { label: "Resolved decisions", value: (a) => String(a.resolved) },
  { label: "Waiting to be scored", value: (a) => String(a.pending_scoring) },
  { label: "Hits", value: (a) => String(a.counts.hit) },
  { label: "Misses", value: (a) => String(a.counts.miss) },
  { label: "Partial", value: (a) => String(a.counts.partial) },
  { label: "Not scorable (no prices)", value: (a) => String(a.counts.unresolvable) },
];

/** Both mandates' outcome accuracy in one table. */
export function AccuracyTab() {
  const { portfolios, portfoliosLoading } = useOutletContext<MandatesOutletContext>();
  const cols = MANDATES.map((m) => ({ mandate: m, portfolio: portfolioOf(portfolios, m) }));
  const results = useQueries({
    queries: cols.map(({ portfolio }) => ({
      queryKey: ["llm-portfolio-accuracy", portfolio?.id ?? null],
      queryFn: () => api<MandateAccuracy>(`/api/llm-portfolio/${portfolio!.id}/accuracy`),
      enabled: portfolio != null,
    })),
  });

  if (portfoliosLoading || results.some((r) => r.isLoading)) {
    return <Skeleton className="h-[260px] w-full" />;
  }
  const data = results.map((r) => r.data ?? null);
  if (data.every((d) => d == null || d.resolved === 0)) {
    return (
      <EmptyState
        icon={Target}
        title="No scored decisions yet"
        description="Decisions are scored once their horizon matures — the weekly scoring job sets a hit/miss/partial verdict."
      />
    );
  }
  const notes = data
    .map((d, i) => (d ? `Mandate ${cols[i].mandate}: ${hitRateSample(d.resolved).note}` : null))
    .filter(Boolean);

  return (
    <div className="space-y-3">
      <Card className="overflow-x-auto p-0">
        <table className="w-full text-sm" data-testid="mandate-accuracy">
          <thead>
            <tr className="border-b border-border text-left text-xs text-text-muted">
              <th className="py-2 pl-4 pr-3 font-normal">Decisions</th>
              {cols.map((c) => (
                <th key={c.mandate} className="py-2 pr-4 text-right font-semibold text-text-primary">
                  Mandate {c.mandate}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map((row) => (
              <tr key={row.label} className="border-b border-border/50 last:border-0">
                <td className="py-2 pl-4 pr-3 text-text-secondary">{row.label}</td>
                {data.map((d, i) => (
                  <td key={cols[i].mandate} className={`py-2 pr-4 text-right font-mono tabular-nums ${d && row.tone ? row.tone(d) : ""}`}>
                    {d ? row.value(d) : cols[i].portfolio ? "—" : "not created"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      {notes.length > 0 && <p className="text-xs text-text-secondary">Hit rate: {notes.join(" · ")}.</p>}
      <p className="text-[11px] text-text-muted">Estimate · not financial advice.</p>
    </div>
  );
}
