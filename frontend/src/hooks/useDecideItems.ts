import { useQuery } from "@tanstack/react-query";
import { api, listNotifications, type PendingRecommendationsResponse } from "../lib/api";

export const PENDING_RECOMMENDATIONS_KEY = ["advisor-pending"] as const;
// Shared with the bell in the top bar, so both read one cached response.
export const NOTIFICATIONS_KEY = ["notifications"] as const;

/** Recommendations waiting for Accept / Reject / Snooze. */
export function usePendingRecommendations() {
  return useQuery({
    queryKey: PENDING_RECOMMENDATIONS_KEY,
    queryFn: () => api<PendingRecommendationsResponse>("/api/portfolio/advisor/pending"),
    refetchInterval: 5 * 60_000,
  });
}

export function useNotifications() {
  return useQuery({ queryKey: NOTIFICATIONS_KEY, queryFn: listNotifications });
}

/** Things waiting for the owner: pending recommendations plus unread notifications. */
export function useDecideCount(): number {
  const pending = usePendingRecommendations();
  const notifications = useNotifications();
  return (pending.data?.total ?? 0) + (notifications.data?.unread_count ?? 0);
}
