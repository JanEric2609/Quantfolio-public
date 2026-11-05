import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { RefreshCw } from "lucide-react";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import {
  getScalableStatus,
  syncScalable,
  type ScalableState,
  type ScalableStatus,
} from "../../lib/api";
import { formatDateTime } from "../../lib/format";
import { BrokerReview } from "./BrokerReview";

const STATE_BADGE: Record<ScalableState, { variant: "success" | "warning" | "danger" | "secondary"; label: string }> = {
  ready: { variant: "success", label: "synced" },
  running: { variant: "secondary", label: "syncing" },
  never_synced: { variant: "secondary", label: "not synced yet" },
  login_required: { variant: "warning", label: "login expired" },
  guard_unattested: { variant: "danger", label: "trade guard missing" },
  not_installed: { variant: "warning", label: "not set up" },
  error: { variant: "danger", label: "sync failed" },
  disabled: { variant: "secondary", label: "off" },
};

export function useScalableStatus() {
  return useQuery<ScalableStatus>({ queryKey: ["scalable-status"], queryFn: getScalableStatus, staleTime: 30_000 });
}

/** Status badge + "Sync Scalable". Renders nothing while the connection is off. */
export function ScalableSyncButton() {
  const queryClient = useQueryClient();
  const status = useScalableStatus();
  const sync = useMutation({
    mutationFn: syncScalable,
    onSuccess: (log) => {
      if (log.state === "warning") toast.warning(log.message);
      else toast.success(log.message);
    },
    onError: (error: Error) => toast.error(error.message),
    onSettled: () => queryClient.invalidateQueries(),
  });
  const data = status.data;
  if (!data?.enabled) return null;
  const badge = data.stale && data.state === "ready" ? { variant: "warning" as const, label: "out of date" } : STATE_BADGE[data.state];
  const detail = [
    data.last_success_at ? `Last successful sync ${formatDateTime(data.last_success_at)}` : "No successful sync yet",
    data.message,
    "Read-only: Quantfolio cannot trade at Scalable.",
  ].filter(Boolean).join(" · ");
  return (
    <div className="flex items-center gap-2">
      <Badge variant={badge.variant} title={detail} aria-label={`Scalable: ${badge.label}. ${detail}`}>
        Scalable: {badge.label}
      </Badge>
      <Button
        type="button"
        variant="outline"
        onClick={() => sync.mutate()}
        disabled={sync.isPending || data.state === "running"}
        title="Read holdings, cash and transactions from Scalable Capital (read-only)"
      >
        <RefreshCw className={sync.isPending ? "h-4 w-4 animate-spin" : "h-4 w-4"} aria-hidden="true" />
        Sync Scalable
      </Button>
      {["login_required", "guard_unattested", "not_installed", "error"].includes(data.state) ? (
        <Button asChild variant="ghost" size="sm">
          <Link to="/settings/bank?connection=scalable">Fix</Link>
        </Button>
      ) : null}
    </div>
  );
}

/** Scalable positions also entered by hand (the shared review, anchored at #scalable-review). */
export function ScalableReview() {
  const status = useScalableStatus();
  return <BrokerReview source="scalable" enabled={Boolean(status.data?.positions)} />;
}
