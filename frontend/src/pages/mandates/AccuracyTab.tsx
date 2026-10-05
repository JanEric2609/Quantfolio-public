import { useOutletContext } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Target } from "lucide-react";
import { Skeleton } from "../../components/ui/skeleton";
import { EmptyState } from "../../components/composed/EmptyState";
import { KpiTile } from "../../components/composed/KpiTile";
import { hitRateSample, TOO_EARLY } from "../../lib/sampleSize";
import { formatPercent } from "../../lib/format";
import { api, type MandateAccuracy } from "../../lib/api";
import type { MandatesOutletContext } from "./MandatesHub";

export function AccuracyTab() {
  const { portfolioId, mandate } = useOutletContext<MandatesOutletContext>();

  const accuracyQuery = useQuery({
    queryKey: ["llm-portfolio-accuracy", portfolioId],
    queryFn: () => api<MandateAccuracy>(`/api/llm-portfolio/${portfolioId}/accuracy`),
    enabled: portfolioId != null,
  });

  if (portfolioId == null) {
    return (
      <EmptyState
        icon={Target}
        title={`No Mandate ${mandate} portfolio yet`}
        description="Create the mandate on the Overview tab before accuracy can be scored."
      />
    );
  }

  if (accuracyQuery.isLoading) {
    return (
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {[...Array(6)].map((_, i) => <Skeleton key={i} className="h-[88px] w-full" />)}
      </div>
    );
  }

  const data = accuracyQuery.data;
  if (!data || data.resolved === 0) {
    return (
      <EmptyState
        icon={Target}
        title="No scored decisions yet"
        description="Decisions are scored once their horizon matures — the weekly scoring job sets a hit/miss/partial verdict."
      />
    );
  }

  const sample = hitRateSample(data.resolved);
  const hitRateTone = !sample.enough || data.hit_rate == null ? "neutral" : data.hit_rate >= 0.5 ? "good" : "bad";

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <KpiTile
          label="Hit Rate"
          value={!sample.enough ? TOO_EARLY : data.hit_rate != null ? formatPercent(data.hit_rate) : "—"}
          tone={hitRateTone}
        />
        <KpiTile label="Resolved Decisions" value={data.resolved} mono />
        <KpiTile label="Pending Scoring" value={data.pending_scoring} mono />
        <KpiTile label="Hits" value={data.counts.hit} tone="good" mono />
        <KpiTile label="Misses" value={data.counts.miss} tone="bad" mono />
        <KpiTile label="Partial" value={data.counts.partial} tone="warn" mono />
      </div>
      <p className="text-xs text-text-secondary">Hit rate: {sample.note}.</p>
      {data.counts.unresolvable > 0 && (
        <p className="text-xs text-text-muted">
          {data.counts.unresolvable} decision(s) could not be scored (missing price data) and are
          excluded from the hit rate.
        </p>
      )}
      <p className="text-[11px] text-text-muted">Estimate · not financial advice.</p>
    </div>
  );
}
