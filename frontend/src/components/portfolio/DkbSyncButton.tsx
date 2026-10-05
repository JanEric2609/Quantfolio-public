import { Link } from "react-router-dom";
import { RefreshCw } from "lucide-react";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Switch } from "../ui/switch";
import { useDkbSync } from "../../hooks/useDkbSync";
import { useUiStore } from "../../lib/store";

type SyncState = "pending_tan" | "waiting_for_push" | "needs_manual_tan" | "confirmed" | "failed" | "cached" | "expired" | "idle";

type BadgeStyle = { variant: "success" | "warning" | "danger"; label: string };

// Map sync state to a colored Badge.  Idle and unrecognised states return null
// so the caller can omit the badge entirely.
function syncStateBadge(state: SyncState): BadgeStyle | null {
  switch (state) {
    case "confirmed":
      return { variant: "success", label: "confirmed" };
    case "cached":
      return { variant: "success", label: "cached" };
    case "failed":
    case "expired":
      return { variant: "danger", label: state };
    case "pending_tan":
    case "waiting_for_push":
    case "needs_manual_tan":
      return { variant: "warning", label: state.replace(/_/g, " ") };
    default:
      return null;
  }
}

export function DkbSyncButton({ showState = false, showTestPid = true }: {
  showState?: boolean;
  /** Hide the "Test PID" connectivity switch where it would only add noise. */
  showTestPid?: boolean;
}) {
  const sync = useDkbSync();
  const useTestPid = useUiStore((s) => s.dkbUseTestPid);
  const setUseTestPid = useUiStore((s) => s.setDkbUseTestPid);
  // After a same-day cached short-circuit or a failed/expired attempt, offer a
  // force re-sync that bypasses the once-per-day cache guard.
  const canForce = ["cached", "failed", "expired"].includes(sync.state);
  // Failed/expired sessions deserve a one-click escape hatch to the diagnostics panel.
  const showDiagnosticsLink = ["failed", "expired"].includes(sync.state);
  const badge = showState ? syncStateBadge(sync.state as SyncState) : null;
  const errorMessage = ["failed", "expired"].includes(sync.state) ? sync.session?.message : undefined;
  // Phone: a full-width stack (Sync button + Test-PID switch on one row, the status
  // line under it). The status row is always rendered when `showState` is on, with a
  // fixed minimum height, so the "waiting for push" badge appearing does not push the
  // layout down. From `sm` the badge goes back in front of the button, inline.
  return (
    <div className="flex w-full min-w-0 flex-col gap-2 sm:w-auto sm:flex-row sm:flex-wrap sm:items-center sm:justify-end">
      {showState && (
        <div className="order-last flex min-h-6 items-center sm:order-first sm:min-h-0" role="status" aria-live="polite">
          {badge && (
            <Badge
              variant={badge.variant}
              title={errorMessage}
              aria-label={errorMessage ? `${badge.label}: ${errorMessage}` : badge.label}
            >
              {badge.label}
            </Badge>
          )}
        </div>
      )}
      <div className="flex min-w-0 items-center gap-2">
        <Button
          type="button"
          variant="outline"
          className="min-w-0 flex-1 sm:flex-none"
          onClick={() => sync.start({ force: false, useTestProductId: useTestPid })}
          disabled={sync.isPending}
        >
          <RefreshCw className={sync.isPending ? "mr-2 h-4 w-4 animate-spin" : "mr-2 h-4 w-4"} />
          Sync DKB
        </Button>
        {showTestPid && (
          <label
            className="flex min-h-11 shrink-0 cursor-pointer select-none items-center gap-1.5 sm:min-h-0"
            title="Uses a shared community test product ID for connectivity only — not your registered ID."
          >
            <Badge variant={useTestPid ? "default" : "secondary"} className="text-[10px] px-1.5 py-0 leading-tight">
              Test PID
            </Badge>
            <Switch
              checked={useTestPid}
              onCheckedChange={(checked) => setUseTestPid(checked)}
              aria-label="Use the shared test product ID"
              className="h-4 w-7"
            />
          </label>
        )}
      </div>
      {canForce && (
        <Button
          type="button"
          variant="ghost"
          className="w-full sm:w-auto"
          onClick={() => sync.start({ force: true, useTestProductId: useTestPid })}
          disabled={sync.isPending}
          title="Ignore the once-per-day cache and sync again now"
        >
          Force re-sync
        </Button>
      )}
      {showDiagnosticsLink && (
        <Button asChild variant="outline" size="sm" className="w-full sm:w-auto" disabled={sync.isPending} title="Open Control Center → Status → Bank sync">
          <Link to="/settings/status#dkb">Diagnostics</Link>
        </Button>
      )}
    </div>
  );
}
