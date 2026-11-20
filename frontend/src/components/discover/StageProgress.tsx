import { useEffect, useRef, useCallback, useState } from "react";
import { Loader2, XCircle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { api, type DiscoverRunDetail, type DiscoverRunDebug } from "../../lib/api";
import { toast } from "sonner";

interface StageProgressProps {
  runId: string;
  onComplete: (detail: DiscoverRunDetail) => void;
  onError: (message: string) => void;
  onCancelled?: (detail: DiscoverRunDetail) => void;
}

const MAX_POLL_COUNT = 600;
const MAX_POLL_FAILURES = 5;
const POLL_INTERVAL_MS = 3000;

export function StageProgress({ runId, onComplete, onError, onCancelled }: StageProgressProps) {
  const [detail, setDetail] = useState<DiscoverRunDetail | null>(null);
  const [copyingDiagnostics, setCopyingDiagnostics] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollCountRef = useRef(0);
  const pollFailCountRef = useRef(0);
  const isFetchingRef = useRef(false);
  const onCompleteRef = useRef(onComplete);
  const onErrorRef = useRef(onError);
  const onCancelledRef = useRef(onCancelled);
  onCompleteRef.current = onComplete;
  onErrorRef.current = onError;
  onCancelledRef.current = onCancelled;

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    pollCountRef.current = 0;
    pollFailCountRef.current = 0;
  }, []);

  useEffect(() => {
    pollCountRef.current = 0;
    pollFailCountRef.current = 0;

    pollRef.current = setInterval(async () => {
      pollCountRef.current += 1;
      if (isFetchingRef.current) return; // skip if previous request still in-flight
      isFetchingRef.current = true;

      if (pollCountRef.current > MAX_POLL_COUNT) {
        stopPolling();
        toast.error("Discovery run is taking longer than expected.");
        isFetchingRef.current = false;
        return;
      }

      try {
        const data = await api<DiscoverRunDetail>(`/api/discover/runs/${runId}`);
        pollFailCountRef.current = 0;
        setDetail(data);

        if (data.status === "completed" || data.status === "failed" || data.status === "cancelled") {
          stopPolling();
          if (data.status === "cancelled") {
            toast.info("Discovery run was cancelled");
            onCancelledRef.current?.(data);
            return;
          }
          if (data.status === "completed") {
            toast.success("Discovery run completed");
            onCompleteRef.current(data);
          } else {
            toast.error(data.error_message || "Discovery run failed");
            onErrorRef.current(data.error_message || "Discovery run failed");
          }
        }
      } catch {
        pollFailCountRef.current += 1;
        if (pollFailCountRef.current >= MAX_POLL_FAILURES) {
          stopPolling();
          toast.error("Lost connection to server. Check back later.");
          onErrorRef.current("Lost connection to server");
        }
      } finally {
        isFetchingRef.current = false;
      }
    }, POLL_INTERVAL_MS);

    return () => stopPolling();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, stopPolling]);

  type StageInfo = {
    state?: string;
    message?: string;
    total_candidates?: number;
    processed_candidates?: number;
    current_candidate?: string;
    current_stage?: string;
    elapsed_seconds?: number;
    warmup_done?: number;
    warmup_total?: number;
  };

  const stageData = detail?.stage_json?.discover as StageInfo | undefined;

  // During the price-cache warm-up the candidate counter is intentionally 0,
  // so drive the bar from warm-up progress to avoid a frozen-at-0% appearance.
  const isWarming =
    stageData?.current_stage === "warming_price_cache" && (stageData?.warmup_total ?? 0) > 0;

  const candidateProgress = isWarming
    ? {
        processed: stageData?.warmup_done ?? 0,
        total: stageData?.warmup_total ?? 0,
        symbol: "",
        label: "Warming price history",
      }
    : stageData?.total_candidates
      ? {
          processed: stageData.processed_candidates ?? 0,
          total: stageData.total_candidates ?? 0,
          symbol: stageData.current_candidate ?? "",
          label: "",
        }
      : null;

  const showCancelled = stageData?.state === "cancelled";

  const copyDiagnostics = useCallback(async () => {
    if (copyingDiagnostics) return; // guard against concurrent requests on rapid clicks
    setCopyingDiagnostics(true);
    try {
      const data = await api<DiscoverRunDebug>(`/api/discover/runs/${runId}/debug`);
      const text = JSON.stringify(data, null, 2);
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        // clipboard unavailable (insecure context) — fall back to console
      }
      // eslint-disable-next-line no-console
      console.log("[discover debug]", data);
      toast.success(data?.hint ? `Diagnostics copied — ${data.hint}` : "Diagnostics copied to clipboard");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to fetch diagnostics");
    } finally {
      setCopyingDiagnostics(false);
    }
  }, [runId, copyingDiagnostics]);

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-2">
            {showCancelled ? (
              <XCircle className="w-4 h-4 text-danger" />
            ) : (
              <Loader2 className="w-4 h-4 animate-spin text-accent" />
            )}
            Discovery Run {showCancelled ? "Cancelled" : "in Progress"}
          </span>
          <button
            type="button"
            onClick={copyDiagnostics}
            disabled={copyingDiagnostics}
            className="rounded border border-line px-2 py-0.5 text-[10px] font-normal text-text-muted hover:text-text-primary disabled:opacity-50 disabled:cursor-not-allowed"
            title="Fetch run diagnostics and copy to clipboard"
          >
            {copyingDiagnostics ? "Copying…" : "Copy diagnostics"}
          </button>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {!stageData ? (
          <div className="flex items-center gap-2 text-sm text-text-secondary">
            <Loader2 className="w-4 h-4 animate-spin" />
            Initialising pipeline...
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              {showCancelled ? (
                <XCircle className="w-4 h-4 text-danger" />
              ) : (
                <Loader2 className="w-4 h-4 text-accent animate-spin" />
              )}
              <div className="flex-1">
                <div className="text-sm font-medium capitalize">
                  {stageData.state?.replace(/_/g, " ")}
                </div>
                {stageData.message && (
                  <div className="text-xs text-text-muted">{stageData.message}</div>
                )}
                {candidateProgress && (
                  <div className="mt-1">
                    <div className="flex items-center justify-between text-xs text-text-muted mb-1">
                      <span>
                        {candidateProgress.label
                          ? `${candidateProgress.label}: ${candidateProgress.processed} / ${candidateProgress.total}`
                          : candidateProgress.symbol
                            ? `Processing ${candidateProgress.symbol}`
                            : `${candidateProgress.processed} / ${candidateProgress.total}`}
                      </span>
                      <span>{Math.round((candidateProgress.processed / candidateProgress.total) * 100)}%</span>
                    </div>
                    <div className="h-1.5 w-full bg-surface-2 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-accent rounded-full transition-all duration-500"
                        style={{ width: `${(candidateProgress.processed / candidateProgress.total) * 100}%` }}
                      />
                    </div>
                  </div>
                )}
                {showCancelled && (
                  <div className="text-xs text-danger mt-1">
                    Run was cancelled — showing partial results
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
