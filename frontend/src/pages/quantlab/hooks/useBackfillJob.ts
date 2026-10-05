import { useCallback, useEffect, useRef, useState } from "react";
import { enqueueBackfillHoldings, getBackfillJob, type BackfillJob } from "../../../lib/api";

const POLL_INTERVAL_MS = 2500;
const MAX_POLLS = 240; // ~10 minutes at 2.5s

/**
 * Kicks off an async holdings backfill (proposal P4) and polls its status to a
 * terminal state, mirroring the LLM-review polling UX. Returns the current job,
 * a start() trigger, whether it is running, and any error.
 */
export function useBackfillJob(onDone?: () => void) {
  const [job, setJob] = useState<BackfillJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const polls = useRef(0);

  const clear = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const poll = useCallback(
    (jobId: string) => {
      getBackfillJob(jobId)
        .then((next) => {
          setJob(next);
          polls.current += 1;
          if (next.status === "succeeded" || next.status === "failed") {
            clear();
            if (next.status === "failed") {
              setError(next.error_message ?? "Backfill failed");
            } else {
              onDone?.();
            }
            return;
          }
          if (polls.current >= MAX_POLLS) {
            clear();
            setError("Backfill is taking longer than expected; check back later.");
            return;
          }
          timer.current = setTimeout(() => poll(jobId), POLL_INTERVAL_MS);
        })
        .catch((e) => {
          clear();
          setError(e instanceof Error ? e.message : "Failed to poll backfill status");
        });
    },
    [clear, onDone]
  );

  const start = useCallback(
    (days = 1825) => {
      setError(null);
      polls.current = 0;
      enqueueBackfillHoldings(days)
        .then((handle) => {
          setJob(handle);
          timer.current = setTimeout(() => poll(handle.job_id), POLL_INTERVAL_MS);
        })
        .catch((e) => {
          setError(e instanceof Error ? e.message : "Failed to start backfill");
        });
    },
    [poll]
  );

    useEffect(() => {
    return () => clear();
  }, [clear]);

  const isRunning = job != null && (job.status === "queued" || job.status === "running");

  return { job, error, isRunning, start };
}
