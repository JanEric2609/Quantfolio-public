import { WifiOff } from "lucide-react";
import { useOnlineStatus } from "../../hooks/useOnlineStatus";

/**
 * Shown while the browser reports no connection. The service worker then
 * answers plan, wealth and holdings from its last saved copy, so the figures
 * on screen can be out of date.
 */
export function OfflineBanner() {
  const online = useOnlineStatus();
  if (online) return null;
  return (
    <div
      role="status"
      className="flex items-start gap-2 border-b border-warn/40 bg-warn/10 px-4 py-2 text-xs text-warn"
    >
      <WifiOff className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>You are offline. Plan, wealth and holdings show the last saved data and may be out of date.</span>
    </div>
  );
}
