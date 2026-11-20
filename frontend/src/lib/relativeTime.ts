import { formatDistanceToNowStrict } from "date-fns";

/** "3 hours ago" for an ISO timestamp; empty when there is none or it does not parse. */
export function timeAgo(iso?: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return formatDistanceToNowStrict(date, { addSuffix: true });
}
