import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type DkbSync } from "../lib/api";

const POLLING_STATES = ["pending_tan", "waiting_for_push"];

// Maximum time (ms) we keep polling for a TAN confirmation before giving up.
// This prevents an infinite loop if the backend becomes unreachable while awaiting push TAN.
const TAN_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes

export function useDkbSync() {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<DkbSync | null>(null);
  // Timestamp (ms) when polling started; used to enforce the TAN timeout.
  const pollingStartedAt = useRef<number | null>(null);

  const sync = useMutation({
    mutationFn: (opts?: { force?: boolean; useTestProductId?: boolean }) => {
      const params = new URLSearchParams();
      if (opts?.force) params.set("force", "true");
      if (opts?.useTestProductId) params.set("use_test_product_id", "true");
      const qs = params.toString();
      return api<DkbSync>(`/api/dkb/sync${qs ? `?${qs}` : ""}`, { method: "POST" });
    },
    onSuccess: (next) => {
      setSession(next);
      // Record when we entered a polling state so the timeout countdown can start.
      if (POLLING_STATES.includes(next.state)) {
        pollingStartedAt.current = Date.now();
      }
      toast.message(next.message);
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const isPolling = Boolean(session?.session_id && POLLING_STATES.includes(session.state));

  const status = useQuery({
    queryKey: ["dkb-sync", session?.session_id],
    queryFn: () => api<DkbSync>(`/api/dkb/sync/status?session_id=${session?.session_id}`),
    enabled: isPolling,
    refetchInterval: session?.next_poll_after_seconds ? session.next_poll_after_seconds * 1000 : 5000,
  });

  // The push TAN is approved in the DKB app, usually on the very phone running this
  // page. While that app is in front, the browser throttles the poll timer (and
  // TanStack pauses `refetchInterval` in a hidden tab), so the moment the user
  // switches back, ask for the status right away instead of waiting out the interval.
  const refetchStatus = status.refetch;
  useEffect(() => {
    if (!isPolling) return;
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") void refetchStatus();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [isPolling, refetchStatus]);

  useEffect(() => {
    if (!status.data) return;
    setSession(status.data);
    if (!POLLING_STATES.includes(status.data.state)) {
      // Left polling state normally — reset timeout tracker.
      pollingStartedAt.current = null;
      if (status.data.state === "confirmed") {
        toast.success(status.data.message);
      } else if (status.data.state === "failed" || status.data.state === "expired") {
        toast.error(status.data.message);
      }
      queryClient.invalidateQueries();
    }
  }, [queryClient, status.data]);

  // Client-side TAN timeout: if we have been polling for longer than TAN_TIMEOUT_MS,
  // transition the session to a synthetic "expired" state to stop polling and alert the user.
  useEffect(() => {
    if (!isPolling) return;

    const interval = setInterval(() => {
      if (pollingStartedAt.current !== null && Date.now() - pollingStartedAt.current >= TAN_TIMEOUT_MS) {
        pollingStartedAt.current = null;
        setSession((prev) =>
          prev
            ? { ...prev, state: "expired", message: "TAN confirmation timed out. Please try again." }
            : prev
        );
        toast.error("DKB TAN confirmation timed out after 5 minutes.");
      }
    }, 5000);

    return () => clearInterval(interval);
  }, [isPolling]);

  return {
    start: (opts?: { force?: boolean; useTestProductId?: boolean }) => sync.mutate(opts ?? {}),
    isPending: sync.isPending || status.isFetching,
    session,
    state: session?.state ?? "idle",
  };
}
