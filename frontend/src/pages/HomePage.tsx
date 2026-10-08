import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, CalendarCheck, Inbox, PieChart } from "lucide-react";
import { AttentionList } from "../components/composed/AttentionList";
import { PageHeader } from "../components/composed/PageHeader";
import { DkbSyncButton } from "../components/portfolio/DkbSyncButton";
import { ScalableSyncButton } from "../components/portfolio/ScalableSync";
import { RegimeChip } from "../components/regime/RegimeChip";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Skeleton } from "../components/ui/skeleton";
import { useNotifications, usePendingRecommendations } from "../hooks/useDecideItems";
import { api, getMonthlyPlan, getSettings, type WealthSummary } from "../lib/api";
import { formatCurrency } from "../lib/format";
import { timeAgo } from "../lib/relativeTime";
import { useAttention } from "./settings/lib/schema";

const NEEDS_YOU_SHOWN = 5;
const DKB_STALE_HOURS = 24;
// Same default as the worker's Scalable sync job (scalable_sync_hours).
const DEFAULT_SCALABLE_SYNC_HOURS = 6;

/** Old, or never synced. A broker with a failing sync shows up as a problem and is handled by the caller. */
function isStale(lastSynced: string | null, maxHours: number): boolean {
  if (!lastSynced) return true;
  const synced = new Date(lastSynced).getTime();
  return Number.isNaN(synced) || Date.now() - synced > maxHours * 3600_000;
}

function InlineError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="flex items-center justify-between gap-3 rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
      <span>{message}</span>
      <Button type="button" variant="outline" size="sm" className="h-10 shrink-0" onClick={onRetry}>Retry</Button>
    </div>
  );
}

/** The combined book, one row per broker (and hand-entered holdings), with each one's last sync. */
function BrokerSplit({ wealth, syncNeeded }: { wealth: WealthSummary; syncNeeded: Record<string, boolean> }) {
  const rows = wealth.by_broker ?? [];
  if (rows.length === 0) return null;
  const total = wealth.total_value || 1;
  return (
    <ul aria-label="Where the book is held" className="divide-y divide-border rounded-md border border-border">
      {rows.map((row) => (
        <li key={row.source} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2 text-sm">
          <span className="min-w-0 font-medium text-text-primary sm:w-36">{row.label}</span>
          <span className="tabular-nums text-text-primary">{formatCurrency(row.total, { currency: wealth.currency })}</span>
          <span className="text-xs tabular-nums text-text-muted">{Math.round((100 * row.total) / total)} %</span>
          <span className="text-xs text-text-muted">
            {formatCurrency(row.securities, { currency: wealth.currency })} securities ·{" "}
            {formatCurrency(row.cash, { currency: wealth.currency })} cash
          </span>
          {row.source !== "manual" && (
            <span className="text-xs text-text-secondary sm:ml-auto">
              {row.last_synced ? `synced ${timeAgo(row.last_synced)}` : "never synced"}
            </span>
          )}
          {syncNeeded[row.source] && (
            <div className="flex w-full min-w-0 pt-1.5">
              {row.source === "dkb" ? <DkbSyncButton showState showTestPid={false} /> : <ScalableSyncButton />}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function StatusStrip() {
  const wealth = useQuery({ queryKey: ["wealth"], queryFn: () => api<WealthSummary>("/api/portfolio/wealth") });
  const settings = useQuery({ queryKey: ["settings"], queryFn: getSettings });
  const attention = useAttention();

  const syncHours = Number(settings.data?.settings?.scalable_sync_hours) || DEFAULT_SCALABLE_SYNC_HOURS;
  // A sync button appears only when that broker is stale or its sync failed.
  const failed = (source: string) =>
    (attention.data?.items ?? []).some((i) => i.severity !== "info" && i.id.toLowerCase().includes(source));
  const syncNeeded: Record<string, boolean> = {};
  for (const row of wealth.data?.by_broker ?? []) {
    if (row.source === "dkb") syncNeeded.dkb = isStale(row.last_synced, DKB_STALE_HOURS) || failed("dkb");
    if (row.source === "scalable") syncNeeded.scalable = isStale(row.last_synced, 2 * syncHours) || failed("scalable");
  }

  return (
    <section aria-label="Status" className="space-y-4 rounded-lg border border-border bg-surface p-4">
      {wealth.isError ? (
        <InlineError message="Could not load your portfolio value." onRetry={() => wealth.refetch()} />
      ) : (
        <div className="min-w-0 space-y-1">
          <div className="text-xs text-text-secondary">Total value</div>
          <div className="min-h-8 text-2xl font-semibold tabular-nums text-text-primary">
            {wealth.isLoading ? <Skeleton className="h-8 w-32" /> : formatCurrency(wealth.data?.total_value, { currency: wealth.data?.currency ?? "EUR" })}
          </div>
        </div>
      )}
      {wealth.data && <BrokerSplit wealth={wealth.data} syncNeeded={syncNeeded} />}
    </section>
  );
}

/** Only real (non-info) problems; renders nothing while loading or when there are none. */
function NeedsYou() {
  const attention = useAttention();
  const problems = (attention.data?.items ?? []).filter((item) => item.severity !== "info");
  const shown = problems.slice(0, NEEDS_YOU_SHOWN);

  if (attention.isLoading) return null;
  if (!attention.isError && problems.length === 0) return null;
  return (
    <section aria-labelledby="home-needs-you" className="space-y-3">
      <h2 id="home-needs-you" className="font-display text-lg font-semibold text-text-primary">Needs you</h2>
      {attention.isError ? (
        <InlineError message="Could not load the list." onRetry={() => attention.refetch()} />
      ) : (
        <>
          <AttentionList items={shown} />
          {problems.length > shown.length && (
            <Link to="/settings" className="inline-flex items-center gap-1 text-sm font-medium text-accent hover:underline">
              {problems.length - shown.length} more in the Control Center <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </Link>
          )}
        </>
      )}
    </section>
  );
}

/** A card only when a decision is waiting; research-only ideas get one muted line. */
function DecideTeaser() {
  const pending = usePendingRecommendations();
  const notifications = useNotifications();
  const waiting = pending.data?.total ?? 0;
  const research = pending.data?.research_count ?? 0;
  const unread = notifications.data?.unread_count ?? 0;
  const tickers = (pending.data?.items ?? []).map((item) => item.ticker).filter(Boolean).slice(0, 4);

  if (waiting === 0) {
    if (research === 0) return null;
    return (
      <p className="text-sm text-text-muted">
        No decision waiting. {research} idea{research === 1 ? " is" : "s are"} in Discover as research, none has
        passed the evidence test yet.
      </p>
    );
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="flex items-center gap-2 text-[1rem]"><Inbox className="h-4 w-4" aria-hidden="true" /> Decide</CardTitle>
        <Badge variant="danger">{waiting}</Badge>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-text-secondary">
          {waiting} recommendation{waiting === 1 ? "" : "s"} to decide
          {unread > 0 ? `, ${unread} unread alert${unread === 1 ? "" : "s"}` : ""}.
        </p>
        {tickers.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {tickers.map((ticker) => <Badge key={ticker} variant="outline" className="font-mono">{ticker}</Badge>)}
          </div>
        )}
        <Button asChild className="h-11 w-full sm:w-auto">
          <Link to="/decide">Review <ArrowRight className="ml-1.5 h-4 w-4" aria-hidden="true" /></Link>
        </Button>
      </CardContent>
    </Card>
  );
}

function ThisMonthCard() {
  const plan = useQuery({ queryKey: ["plan", "month"], queryFn: getMonthlyPlan });
  const data = plan.data;

  return (
    <Card className={data?.no_change ? "border-success/40" : undefined}>
      <CardHeader className="space-y-2 pb-3">
        <CardTitle className="flex items-center gap-2 text-[1rem]"><CalendarCheck className="h-4 w-4" aria-hidden="true" /> This month</CardTitle>
        {data && (
          <div className="flex flex-wrap items-center gap-2">
            {data.no_change && <Badge variant="success">No change</Badge>}
            <Badge variant={data.never_sells ? "secondary" : "warning"}>{data.never_sells ? "No sale" : "Rebalance"}</Badge>
          </div>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {plan.isLoading ? (
          <Skeleton className="h-16 w-full" />
        ) : plan.isError || !data ? (
          <InlineError message="Could not load this month's plan." onRetry={() => plan.refetch()} />
        ) : (
          <p className="text-[1rem] leading-snug text-text-primary">{data.headline}</p>
        )}
        <Button asChild variant="outline" className="h-11 w-full sm:w-auto">
          <Link to="/plan">Open the plan <ArrowRight className="ml-1.5 h-4 w-4" aria-hidden="true" /></Link>
        </Button>
      </CardContent>
    </Card>
  );
}

/**
 * Home: a calm glance. What your money is worth, whether the data is fresh,
 * what to do this month. Problems and decisions appear only when there are some.
 * The detailed overview stays at /dashboard.
 */
export function HomePage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Home"
        subtitle="Your money at a glance."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <RegimeChip />
            <Button asChild variant="outline" size="sm" className="h-10 sm:h-8">
              <Link to="/dashboard"><PieChart className="mr-2 h-4 w-4" aria-hidden="true" /> Detailed overview</Link>
            </Button>
          </div>
        }
      />
      <StatusStrip />
      <ThisMonthCard />
      <NeedsYou />
      <DecideTeaser />
    </div>
  );
}
