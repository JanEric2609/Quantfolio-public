import { useQuery } from "@tanstack/react-query";
import { api, type RegimeCurrent, type RegimeLabel } from "../../lib/api";
import { Badge } from "../ui/badge";
import { formatDate } from "../../lib/format";

const LABEL_VARIANT: Record<RegimeLabel, "success" | "danger" | "secondary"> = {
  bull: "success",
  bear: "danger",
  sideways: "secondary",
};

/**
 * Compact market-regime badge for the Dashboard header: shows the current
 * regime label and, when the crisis gate has fired, a "Crisis" indicator.
 * A snapshot older than the backend TTL renders greyed-out with a
 * "stale · Xh ago" tooltip so a dead pipeline never reads as live analysis.
 * Renders nothing until a regime snapshot exists (backend-first, minimal).
 */
export function RegimeBadge() {
  const { data } = useQuery({
    queryKey: ["regime-current"],
    queryFn: () => api<RegimeCurrent>("/api/data/regime/current"),
    retry: false,
  });

  if (!data?.label) return null;

  const stale = data.stale === true;
  const hoursAgo = data.age_hours != null ? Math.round(data.age_hours) : null;
  const variant = LABEL_VARIANT[data.label] ?? "secondary";
  const title = stale
    ? `stale · ${hoursAgo ?? "?"}h ago`
    : data.ts
      ? `Regime as of ${formatDate(data.ts)}`
      : "Current market regime";

  return (
    <div className="flex items-center gap-2" title={title}>
      <span className="text-xs text-text-secondary">Regime</span>
      <Badge variant={variant} className={`capitalize${stale ? " opacity-50 grayscale" : ""}`}>
        {data.label}
      </Badge>
      {data.crisis && <Badge variant="warning">Crisis</Badge>}
    </div>
  );
}
