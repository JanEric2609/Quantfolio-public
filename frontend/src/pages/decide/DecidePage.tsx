import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCheck, CheckCircle2, ExternalLink, Hourglass } from "lucide-react";
import { PageHeader } from "../../components/composed/PageHeader";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Skeleton } from "../../components/ui/skeleton";
import {
  NOTIFICATIONS_KEY,
  PENDING_RECOMMENDATIONS_KEY,
  useNotifications,
  usePendingRecommendations,
} from "../../hooks/useDecideItems";
import {
  api,
  markAllNotificationsRead,
  markNotificationRead,
  type AcceptedRecommendations,
  type AdvisorFeedbackAction,
  type NotificationItem,
  type PendingRecommendationsResponse,
} from "../../lib/api";
import { timeAgo } from "../../lib/relativeTime";
import { RecommendationCard } from "./components/RecommendationCard";

const ACCEPTED_KEY = ["advisor-accepted"] as const;

const DONE_TEXT: Record<AdvisorFeedbackAction, string> = {
  accepted: "Accepted",
  rejected: "Rejected",
  snoozed: "Snoozed for 7 days",
};
const SEVERITY_VARIANT = { critical: "danger", warning: "warning", info: "info" } as const;
const NOTIFICATIONS_SHOWN = 15;

function SectionHeading({ id, children, count }: { id: string; children: React.ReactNode; count?: number }) {
  return (
    <h2 id={id} className="flex items-center gap-2 font-display text-lg font-semibold text-text-primary">
      {children}
      {count ? <Badge variant="secondary">{count}</Badge> : null}
    </h2>
  );
}

function ErrorLine({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="flex items-center justify-between gap-3 rounded-lg border border-danger/30 bg-danger/10 p-4 text-sm text-danger">
      <span>{message}</span>
      <Button type="button" variant="outline" size="sm" className="h-10 shrink-0" onClick={onRetry}>Retry</Button>
    </div>
  );
}

function NotificationRow({ item, onRead, reading }: { item: NotificationItem; onRead: (id: string) => void; reading: boolean }) {
  const internal = item.href?.startsWith("/");
  const title = <span className={item.read_at ? "text-text-secondary" : "font-medium text-text-primary"}>{item.title}</span>;
  return (
    <li className="flex items-start gap-3 px-4 py-3">
      <Badge variant={SEVERITY_VARIANT[item.severity] ?? "info"} className="mt-0.5 shrink-0 capitalize">{item.severity}</Badge>
      <div className="min-w-0 flex-1 text-sm">
        {item.href ? (
          internal
            ? <Link to={item.href} className="hover:underline" onClick={() => !item.read_at && onRead(item.id)}>{title}</Link>
            : <a href={item.href} className="hover:underline" rel="noreferrer">{title}</a>
        ) : title}
        {item.body && <p className="mt-0.5 line-clamp-3 text-text-secondary">{item.body}</p>}
        <span className="text-xs text-text-muted">{timeAgo(item.created_at)}</span>
      </div>
      {!item.read_at && (
        <Button
          type="button"
          variant="ghost"
          className="h-11 shrink-0 px-3 text-xs"
          disabled={reading}
          aria-label={`Mark "${item.title}" as read`}
          onClick={() => onRead(item.id)}
        >
          Mark read
        </Button>
      )}
    </li>
  );
}

function AcceptedSection() {
  const accepted = useQuery({
    queryKey: ACCEPTED_KEY,
    queryFn: () => api<AcceptedRecommendations>("/api/portfolio/advisor/accepted"),
  });
  const waiting = accepted.data?.waiting ?? [];
  const done = (accepted.data?.executed ?? []).slice(0, 5);
  if (accepted.isLoading || (waiting.length === 0 && done.length === 0)) return null;
  return (
    <section aria-labelledby="decide-accepted" className="space-y-3">
      <SectionHeading id="decide-accepted" count={waiting.length}>Accepted</SectionHeading>
      <p className="text-sm text-text-secondary">
        Place the order at your broker. The next DKB or Scalable sync finds the trade and marks it done; there is
        nothing to log by hand.
      </p>
      <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface">
        {waiting.map((item) => (
          <li key={item.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-sm">
            <Hourglass className="h-4 w-4 text-text-muted" aria-hidden="true" />
            <span className="font-mono font-semibold">{item.ticker ?? item.name ?? "—"}</span>
            <span className="text-text-secondary">
              {item.side === "sell" ? "sell" : "buy"} · waiting for the trade
              {item.accepted_at ? ` · accepted ${timeAgo(item.accepted_at)}` : ""}
            </span>
            <span className="ml-auto flex gap-3">
              {item.links.map((link) => (
                <a key={link.broker} href={link.url} target="_blank" rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
                  {link.label} <ExternalLink className="h-3 w-3" aria-hidden="true" />
                </a>
              ))}
            </span>
          </li>
        ))}
        {done.map((item) => (
          <li key={item.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-sm">
            <CheckCircle2 className="h-4 w-4 text-success" aria-hidden="true" />
            <span className="font-mono font-semibold">{item.ticker ?? item.name ?? "—"}</span>
            <span className="text-text-secondary">
              done at {item.broker_label ?? "your broker"}
              {item.units != null ? ` (${item.units > 0 ? "+" : ""}${item.units} units)` : ""}
              {item.executed_at ? `, found ${timeAgo(item.executed_at)}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * The phone's "act on decisions" page: recommendations waiting for Accept /
 * Reject / Snooze, the accepted ones waiting for their trade, and unread
 * alerts. Accepting records your decision and links to the broker; nothing is
 * ordered for you, and the next broker sync finds the trade.
 */
export function DecidePage() {
  const queryClient = useQueryClient();
  const pending = usePendingRecommendations();
  const notifications = useNotifications();

  const decide = useMutation({
    mutationFn: ({ id, action }: { id: string; action: AdvisorFeedbackAction; label: string }) =>
      api(`/api/portfolio/advisor/feedback/${id}?action=${action}`, { method: "POST" }),
    onSuccess: (_result, { id, action, label }) => {
      toast.success(`${DONE_TEXT[action]}: ${label}`);
      // Drop the card now; the refetch below settles the real list and the badge count.
      queryClient.setQueryData<PendingRecommendationsResponse>(PENDING_RECOMMENDATIONS_KEY, (old) =>
        old ? { items: old.items.filter((item) => item.id !== id), total: Math.max(0, old.total - 1) } : old,
      );
      queryClient.invalidateQueries({ queryKey: PENDING_RECOMMENDATIONS_KEY });
      queryClient.invalidateQueries({ queryKey: ACCEPTED_KEY });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const readOne = useMutation({
    mutationFn: (id: string) => markNotificationRead(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_KEY }),
  });
  const readAll = useMutation({
    mutationFn: () => markAllNotificationsRead(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_KEY }),
  });

  const recs = pending.data?.items ?? [];
  const total = pending.data?.total ?? 0;
  const notes = (notifications.data?.items ?? []).slice(0, NOTIFICATIONS_SHOWN);
  const unread = notifications.data?.unread_count ?? 0;

  return (
    <div className="space-y-6">
      <PageHeader title="Decide" subtitle="Recommendations and alerts that are waiting for you." />

      <section aria-labelledby="decide-recs" className="space-y-3">
        <SectionHeading id="decide-recs" count={total}>Recommendations</SectionHeading>
        <p className="text-sm text-text-secondary">
          Accepting records your decision and keeps the links to your broker. Nothing is ordered for you; snoozed
          ones come back after 7 days.
        </p>
        {pending.isLoading ? (
          <div className="space-y-3">
            <Skeleton className="h-40 w-full" />
            <Skeleton className="h-40 w-full" />
          </div>
        ) : pending.isError ? (
          <ErrorLine message="Could not load the recommendations." onRetry={() => pending.refetch()} />
        ) : recs.length === 0 ? (
          <div className="flex items-center gap-3 rounded-lg border border-border bg-surface p-4 text-sm">
            <CheckCircle2 className="h-5 w-5 text-success" aria-hidden="true" />
            <span>Nothing is waiting for a decision.</span>
          </div>
        ) : (
          <>
            <div className="space-y-3">
              {recs.map((rec) => (
                <RecommendationCard
                  key={rec.id}
                  rec={rec}
                  busy={decide.isPending && decide.variables?.id === rec.id}
                  onAction={(action) => decide.mutate({ id: rec.id, action, label: rec.ticker ?? rec.name ?? "recommendation" })}
                />
              ))}
            </div>
            {total > recs.length && (
              <p className="text-center text-xs text-text-secondary">Showing the newest {recs.length} of {total}.</p>
            )}
          </>
        )}
      </section>

      <AcceptedSection />

      <section aria-labelledby="decide-alerts" className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <SectionHeading id="decide-alerts" count={unread}>Alerts</SectionHeading>
          {unread > 0 && (
            <Button
              type="button"
              variant="ghost"
              className="h-11 gap-1.5 px-3 text-xs"
              disabled={readAll.isPending}
              onClick={() => readAll.mutate()}
            >
              <CheckCheck className="h-4 w-4" aria-hidden="true" /> Mark all read
            </Button>
          )}
        </div>
        {notifications.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : notifications.isError ? (
          <ErrorLine message="Could not load your alerts." onRetry={() => notifications.refetch()} />
        ) : notes.length === 0 ? (
          <div className="rounded-lg border border-border bg-surface p-4 text-sm text-text-secondary">No alerts.</div>
        ) : (
          <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface">
            {notes.map((item) => (
              <NotificationRow
                key={item.id}
                item={item}
                reading={readOne.isPending && readOne.variables === item.id}
                onRead={(id) => readOne.mutate(id)}
              />
            ))}
          </ul>
        )}
      </section>

    </div>
  );
}
