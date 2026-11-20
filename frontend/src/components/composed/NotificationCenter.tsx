import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, CheckCheck } from "lucide-react";
import { Link } from "react-router-dom";
import { listNotifications, markAllNotificationsRead, type NotificationItem } from "../../lib/api";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "../ui/popover";

const SEVERITY_VARIANT: Record<string, "danger" | "warning" | "info"> = {
  critical: "danger",
  warning: "warning",
  info: "info",
};

function relativeTime(iso?: string | null): string {
  if (!iso) return "";
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// In-app targets ("/plan", "/settings/status#dkb") go through the router so a tap
// does not reload the whole SPA (and lose its cache); anything else is a real link.
function isInternalHref(href: string): boolean {
  return href.startsWith("/") && !href.startsWith("//");
}

function NotificationRow({ item, onNavigate }: { item: NotificationItem; onNavigate: () => void }) {
  const title = (
    <span className={`text-sm ${item.read_at ? "text-text-muted" : "font-medium"}`}>{item.title}</span>
  );
  return (
    <div className="flex items-start gap-2 py-2 border-b border-border last:border-0">
      <Badge variant={SEVERITY_VARIANT[item.severity] ?? "info"} className="mt-0.5 shrink-0 capitalize">
        {item.severity}
      </Badge>
      <div className="flex-1 min-w-0">
        {item.href ? (
          isInternalHref(item.href) ? (
            <Link to={item.href} className="hover:underline" onClick={onNavigate}>
              {title}
            </Link>
          ) : (
            <a href={item.href} className="hover:underline" target="_blank" rel="noopener noreferrer">
              {title}
            </a>
          )
        ) : (
          title
        )}
        {item.body && <p className="text-xs text-text-muted mt-0.5 line-clamp-2">{item.body}</p>}
        <span className="text-[11px] text-text-muted">{relativeTime(item.created_at)}</span>
      </div>
    </div>
  );
}

export function NotificationCenter() {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["notifications"],
    queryFn: listNotifications,
  });
  const markAll = useMutation({
    mutationFn: markAllNotificationsRead,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const items = data?.items ?? [];
  const unreadCount = data?.unread_count ?? 0;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Notifications" className="relative">
          <Bell className="h-5 w-5" />
          {unreadCount > 0 && (
            <span className="absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[10px] font-semibold text-white">
              {unreadCount > 99 ? "99+" : unreadCount}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80 p-3" align="end">
        <div className="mb-1 flex items-center justify-between">
          <h4 className="text-sm font-semibold">Notifications</h4>
          {unreadCount > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="gap-1 px-2 text-xs"
              onClick={() => markAll.mutate()}
              disabled={markAll.isPending}
            >
              <CheckCheck className="h-3.5 w-3.5" />
              Mark all read
            </Button>
          )}
        </div>
        {isLoading && <p className="text-xs text-text-muted">Loading…</p>}
        {isError && <p className="text-xs text-danger">Failed to load notifications.</p>}
        {!isLoading && !isError && items.length === 0 && (
          <p className="text-xs text-text-muted">No notifications.</p>
        )}
        <div className="max-h-80 overflow-y-auto">
          {items.map((item) => (
            <NotificationRow key={item.id} item={item} onNavigate={() => setOpen(false)} />
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}
