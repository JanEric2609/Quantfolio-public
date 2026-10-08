import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { RotateCcw } from "lucide-react";
import { api, type PaperArchive, type PaperPerformance } from "../../../lib/api";
import { formatCurrency, formatDate, formatPercent } from "../../../lib/format";
import { LineChart } from "../../../components/charts/LineChart";
import { Button } from "../../../components/ui/button";
import { Skeleton } from "../../../components/ui/skeleton";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger,
} from "../../../components/ui/alert-dialog";

const eur = (v?: number | null) => formatCurrency(v, "EUR", { digits: 0 });
const pct = (v?: number | null) => formatPercent(v, { digits: 1, signed: true });

/**
 * One paper run since its inception, next to the same starting value held in
 * MSCI World EUR (decision/paper_portfolio.py:passive_benchmark). Everything is
 * in euros; the run starts at market value, so its return starts at 0. A reset
 * archives the run (trades, snapshots, scorecards) and starts a new one today.
 */
export function PaperRunCard({ portfolioId, invalidate = [] }: { portfolioId: string; invalidate?: string[][] }) {
  const queryClient = useQueryClient();
  const perf = useQuery({
    queryKey: ["paper-performance", portfolioId],
    queryFn: () => api<PaperPerformance>(`/api/paper-portfolio/${portfolioId}/performance`),
  });
  const archives = useQuery({
    queryKey: ["paper-archives", portfolioId],
    queryFn: () => api<PaperArchive[]>(`/api/paper-portfolio/${portfolioId}/archives`),
  });
  const reset = useMutation({
    mutationFn: () =>
      api(`/api/paper-portfolio/${portfolioId}/reset`, { method: "POST", body: JSON.stringify({ confirm: true }) }),
    onSuccess: () => {
      toast.success("Run archived; a new one starts today at market value.");
      for (const key of [["paper-performance", portfolioId], ["paper-archives", portfolioId], ...invalidate]) {
        queryClient.invalidateQueries({ queryKey: key });
      }
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "Reset failed"),
  });

  if (perf.isLoading) return <Skeleton className="h-48 w-full" />;
  if (perf.isError || !perf.data) {
    return <p className="text-xs text-danger">Could not load this run's performance.</p>;
  }
  const { summary: s, benchmark: b, series } = perf.data;
  const runs = archives.data ?? [];
  const points = series.filter((p) => p.value > 0);
  const benchPoints = points.filter((p) => p.benchmark != null);

  return (
    <section className="space-y-3 rounded-md bg-surface p-3 text-xs" aria-label="Paper run against MSCI World EUR">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <p className="text-text-muted">
            Since {s.inception_at ? formatDate(s.inception_at) : "inception"} · started at {eur(s.baseline_value)}
          </p>
          <p className="text-sm text-text-primary">
            This run <strong className="tabular-nums">{pct(s.total_return_pct)}</strong>
            {" · "}
            {b.available ? (
              <>
                same start in MSCI World EUR ({b.symbol}) <strong className="tabular-nums">{pct(b.total_return_pct)}</strong>
                {" · "}
                <span className={(b.excess_return_pct ?? 0) < 0 ? "text-danger" : "text-success"}>
                  {pct(b.excess_return_pct)} vs. the index
                </span>
              </>
            ) : (
              <span className="text-text-muted">no MSCI World EUR prices for the start date yet</span>
            )}
          </p>
          {(s.stale_quotes?.length ?? 0) > 0 && (
            <p className="text-warn">
              No current price for {s.stale_quotes!.join(", ")}: valued at the last known price, not traded until it updates.
            </p>
          )}
          <p className="text-text-muted">
            Dividends credited {eur(s.dividends_eur ?? 0)} (after foreign withholding tax)
            {runs.length > 0 && ` · ${runs.length} earlier run${runs.length === 1 ? "" : "s"} archived`}
          </p>
        </div>
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button size="sm" variant="outline" disabled={reset.isPending}>
              <RotateCcw className="mr-2 h-4 w-4" />
              {reset.isPending ? "Resetting…" : "Reset run"}
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Start a new run today?</AlertDialogTitle>
              <AlertDialogDescription>
                The current run (holdings, trades, snapshots and scorecards) is archived, not deleted. The new run
                starts from your synced book at today's prices, next to the same amount in MSCI World EUR.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction onClick={() => reset.mutate()}>Archive and reset</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>

      {points.length >= 2 ? (
        <LineChart
          ariaLabel="Paper run value and MSCI World EUR"
          className="h-56"
          yFormat={(v) => eur(v)}
          series={[
            { name: "This run", data: points.map((p) => [p.date, p.value]) },
            ...(benchPoints.length >= 2
              ? [{ name: "MSCI World EUR", data: benchPoints.map((p) => [p.date, p.benchmark as number] as [string, number]), color: "#94a3b8" }]
              : []),
          ]}
        />
      ) : (
        <p className="text-text-muted">The chart fills in from the daily snapshots after the first trading days.</p>
      )}

      {runs.length > 0 && (
        <details>
          <summary className="cursor-pointer text-text-muted">Earlier runs</summary>
          <ul className="mt-1 space-y-0.5">
            {runs.map((r) => (
              <li key={r.id} className="tabular-nums">
                {r.inception_at ? formatDate(r.inception_at) : "?"} – {r.archived_at ? formatDate(r.archived_at) : "?"}:{" "}
                {r.last_total_return_pct != null ? pct(r.last_total_return_pct) : "no snapshots"}, {r.trades} trades ({r.reason})
              </li>
            ))}
          </ul>
        </details>
      )}
      <p className="text-text-muted">
        Paper only. Returns are time-weighted in euros, before tax; dividends count when they are paid.
      </p>
    </section>
  );
}
