import { useQuery } from "@tanstack/react-query";
import { TrendingUp } from "lucide-react";
import { LineChart } from "../charts/LineChart";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";
import { Skeleton } from "../ui/skeleton";
import { formatDate, formatNumber, formatPercent } from "../../lib/format";
import { getSkillSummary, getSkillTrend, type DiscoverySkillSnapshot, type DiscoverySkillSummary } from "../../lib/api";
import { hitRateSample, icSample, TOO_EARLY, type WindowInfo } from "../../lib/sampleSize";

function buildTimeSeries(
  data: DiscoverySkillSnapshot[],
  accessor: (s: DiscoverySkillSnapshot) => number | null,
): [number, number][] {
  return data
    .filter((s) => s.snapshot_at && accessor(s) !== null)
    .map((s) => [new Date(s.snapshot_at!).getTime(), accessor(s) as number] as [number, number])
    .sort((a, b) => a[0] - b[0]);
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="min-w-0">
      <p className="text-xs text-text-secondary">{label}</p>
      <p className="font-display text-lg font-semibold tabular-nums text-text-primary">{value}</p>
      {note && <p className="text-xs text-text-muted">{note}</p>}
    </div>
  );
}

/** The headline numbers: judged per date and against the passive core. */
function SkillSummaryRow({ summary }: { summary: DiscoverySkillSummary }) {
  // Below the minimum sample a number reads as information and is noise.
  // Predictions are judged over about a month, so weekly dates overlap: the
  // backend also counts independent months and clusters the interval.
  const win: WindowInfo | undefined =
    summary.independent_windows != null && summary.min_independent_windows != null
      ? {
          windows: summary.independent_windows,
          min: summary.min_independent_windows,
          ciHalfWidth: summary.hit_rate_ci_half_width,
        }
      : undefined;
  const hits = hitRateSample(summary.resolved, win);
  const ics = icSample(summary.ic_dates, win);
  const ic = !ics.enough
    ? TOO_EARLY
    : summary.mean_rank_ic == null
      ? "—"
      : `${formatNumber(summary.mean_rank_ic, { digits: 3 })}${summary.ic_t_stat != null ? ` (t ${formatNumber(summary.ic_t_stat, { digits: 1 })})` : ""}`;
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
      <Stat
        label={`Beat ${summary.benchmark}`}
        value={!hits.enough ? TOO_EARLY : summary.hit_rate == null ? "—" : formatPercent(summary.hit_rate, { decimals: 0 })}
        note={hits.note}
      />
      <Stat
        label={`Average vs ${summary.benchmark}`}
        value={
          !hits.enough
            ? TOO_EARLY
            : summary.mean_excess_return == null ? "—" : formatPercent(summary.mean_excess_return, { decimals: 1 })
        }
        note="per prediction, in EUR"
      />
      <Stat
        label="Rank IC"
        value={ic}
        note={`${ics.note}${ics.enough ? ` with ≥ ${summary.min_ic_names} names` : ""}`}
      />
      <Stat
        label="Pending"
        value={String(summary.pending)}
        note={summary.next_resolve_at ? `next resolves ${formatDate(summary.next_resolve_at)}` : undefined}
      />
    </div>
  );
}

function LoadingSkeleton() {
  return (
    <Card className="p-6 space-y-4">
      <Skeleton className="h-5 w-36" />
      <Skeleton className="h-[200px] w-full rounded-md" />
      <Skeleton className="h-[200px] w-full rounded-md" />
    </Card>
  );
}

export function SkillTrendPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["discover-skill-trend"],
    queryFn: getSkillTrend,
  });
  const { data: summary } = useQuery({
    queryKey: ["discover-skill-summary"],
    queryFn: getSkillSummary,
  });

  if (isLoading) return <LoadingSkeleton />;

  if (error) {
    return (
      <Card className="p-6">
        <p className="text-sm text-danger">Failed to load prediction skill data.</p>
      </Card>
    );
  }

  const snapshots = data ?? [];

  if (snapshots.length === 0) {
    return (
      <Card className="flex flex-col items-center justify-center py-16 px-6 text-center space-y-3">
        <div className="rounded-full bg-surface-2 p-3">
          <TrendingUp size={24} className="text-text-muted" />
        </div>
        <h3 className="text-sm font-semibold text-text-primary">Prediction Skill</h3>
        <p className="text-sm text-text-muted max-w-sm leading-relaxed">
          {summary && summary.pending > 0
            ? `${summary.pending} predictions are waiting for their horizon${
                summary.next_resolve_at ? `; the first resolves on ${formatDate(summary.next_resolve_at)}` : ""
              }. Each is judged against ${summary.benchmark} over the same window.`
            : "No prediction skill data yet — run discovery funnels to build a track record."}
        </p>
      </Card>
    );
  }

  const icData = buildTimeSeries(snapshots, (s) => s.rank_ic);
  const hitData = buildTimeSeries(snapshots, (s) => s.hit_rate);

  const icTimes = icData.map((d) => d[0]);
  const hitTimes = hitData.map((d) => d[0]);
  const allTimes = [...icTimes, ...hitTimes].sort((a, b) => a - b);

  const icRefLine =
    icData.length >= 2
      ? { name: "Zero baseline", data: [[allTimes[0], 0], [allTimes[allTimes.length - 1], 0]] as [number, number][], color: "#6b7280" }
      : null;

  const hitRefLine =
    hitData.length >= 2
      ? { name: "Chance baseline", data: [[allTimes[0], 0.5], [allTimes[allTimes.length - 1], 0.5]] as [number, number][], color: "#6b7280" }
      : null;

  return (
    <Card className="p-6 space-y-6">
      <CardHeader className="p-0">
        <CardTitle className="text-sm font-semibold">Prediction Skill</CardTitle>
      </CardHeader>
      <CardContent className="p-0 space-y-5">
        {summary && <SkillSummaryRow summary={summary} />}
        <div className="space-y-2">
          <p className="text-xs font-medium text-text-secondary uppercase tracking-wide">Rank IC per prediction date</p>
          <LineChart
            series={[
              { name: "Rank IC", data: icData, color: "#FA8001" },
              ...(icRefLine ? [icRefLine] : []),
            ]}
            xType="time"
            yFormat={(v: number) => formatNumber(v, { digits: 2 })}
            area
            height={220}
            ariaLabel="Rank IC over time"
          />
        </div>
        <div className="space-y-2">
          <p className="text-xs font-medium text-text-secondary uppercase tracking-wide">
            Hit rate: beat {summary?.benchmark ?? "the benchmark"}
          </p>
          <LineChart
            series={[
              { name: "Hit Rate", data: hitData, color: "#3b82f6" },
              ...(hitRefLine ? [hitRefLine] : []),
            ]}
            xType="time"
            yFormat={(v: number) => formatPercent(v, { digits: 0 })}
            tooltipFormat={(v: number) => formatPercent(v, { digits: 1 })}
            area
            height={220}
            ariaLabel="Share of predictions that beat the benchmark over time"
          />
        </div>
      </CardContent>
    </Card>
  );
}
