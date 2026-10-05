import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, CalendarCheck, CheckCircle2, Inbox, PieChart } from "lucide-react";
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
import { api, getMacroRegime, getMonthlyPlan, type WealthSummary } from "../lib/api";
import { formatCurrency } from "../lib/format";
import { timeAgo } from "../lib/relativeTime";
import { useAttention } from "./settings/lib/schema";

const NEEDS_YOU_SHOWN = 5;

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 space-y-1">
      <div className="text-xs text-text-secondary">{label}</div>
      <div className="min-h-7 text-lg font-semibold tabular-nums text-text-primary">{children}</div>
    </div>
  );
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
function BrokerSplit({ wealth }: { wealth: WealthSummary }) {
  const rows = wealth.by_broker ?? [];
  if (rows.length === 0) return null;
  const total = wealth.total_value || 1;
  return (
    <ul aria-label="Where the book is held" className="divide-y divide-border rounded-md border border-border">
      {rows.map((row) => (
        <li key={row.source} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2 text-sm">
          <span className="w-36 font-medium text-text-primary">{row.label}</span>
          <span className="tabular-nums text-text-primary">{formatCurrency(row.total, { currency: wealth.currency })}</span>
          <span className="text-xs tabular-nums text-text-muted">{Math.round((100 * row.total) / total)} %</span>
          <span className="text-xs text-text-muted">
            {formatCurrency(row.securities, { currency: wealth.currency })} securities ·{" "}
            {formatCurrency(row.cash, { currency: wealth.currency })} cash
          </span>
          {row.source !== "manual" && (
            <span className="ml-auto text-xs text-text-secondary">
              {row.last_synced ? `synced ${timeAgo(row.last_synced)}` : "never synced"}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

function StatusStrip() {
  const wealth = useQuery({ queryKey: ["wealth"], queryFn: () => api<WealthSummary>("/api/portfolio/wealth") });
  // Same key and options as RegimeChip, so both read one cached snapshot.
  const regime = useQuery({ queryKey: ["macro-regime"], queryFn: getMacroRegime, refetchInterval: 30 * 60 * 1000, retry: 2 });
  const notifications = useNotifications();
  const unread = notifications.data?.unread_count ?? 0;

  return (
    <section aria-label="Status" className="space-y-4 rounded-lg border border-border bg-surface p-4">
      {wealth.isError ? (
        <InlineError message="Could not load your portfolio value." onRetry={() => wealth.refetch()} />
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Stat label="Total value">
            {wealth.isLoading ? <Skeleton className="h-7 w-28" /> : formatCurrency(wealth.data?.total_value, { currency: wealth.data?.currency ?? "EUR" })}
          </Stat>
          <Stat label="Regime">
            {regime.data ? <RegimeChip /> : regime.isLoading ? <Skeleton className="h-6 w-20" /> : <span className="text-text-muted">—</span>}
          </Stat>
          <Stat label="Alerts">
            {notifications.isLoading ? (
              <Skeleton className="h-7 w-8" />
            ) : (
              <Link to="/decide" className={unread > 0 ? "text-danger hover:underline" : "hover:underline"} aria-label={`${unread} unread alerts`}>
                {unread}
              </Link>
            )}
          </Stat>
        </div>
      )}
      {wealth.data && <BrokerSplit wealth={wealth.data} />}
      <div className="flex flex-wrap items-center gap-3">
        <DkbSyncButton showState showTestPid={false} />
        <ScalableSyncButton />
      </div>
    </section>
  );
}

function NeedsYou() {
  const attention = useAttention();
  const problems = (attention.data?.items ?? []).filter((item) => item.severity !== "info");
  const shown = problems.slice(0, NEEDS_YOU_SHOWN);

  return (
    <section aria-labelledby="home-needs-you" className="space-y-3">
      <h2 id="home-needs-you" className="font-display text-lg font-semibold text-text-primary">Needs you</h2>
      {attention.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : attention.isError ? (
        <InlineError message="Could not load the list." onRetry={() => attention.refetch()} />
      ) : problems.length === 0 ? (
        <div className="flex items-center gap-3 rounded-lg border border-border bg-surface p-4 text-sm">
          <CheckCircle2 className="h-5 w-5 text-success" aria-hidden="true" />
          <span>All good. Nothing needs fixing.</span>
        </div>
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

function DecideTeaser() {
  const pending = usePendingRecommendations();
  const notifications = useNotifications();
  const waiting = pending.data?.total ?? 0;
  const unread = notifications.data?.unread_count ?? 0;
  const tickers = (pending.data?.items ?? []).map((item) => item.ticker).filter(Boolean).slice(0, 4);
  const loading = pending.isLoading || notifications.isLoading;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="flex items-center gap-2 text-[1rem]"><Inbox className="h-4 w-4" aria-hidden="true" /> Decide</CardTitle>
        {waiting + unread > 0 && <Badge variant="danger">{waiting + unread}</Badge>}
      </CardHeader>
      <CardContent className="space-y-3">
        {loading ? (
          <Skeleton className="h-12 w-full" />
        ) : pending.isError && notifications.isError ? (
          <p className="text-sm text-danger">Could not load what is waiting for you.</p>
        ) : waiting + unread === 0 ? (
          <p className="text-sm text-text-secondary">Nothing is waiting for a decision.</p>
        ) : (
          <>
            <p className="text-sm text-text-secondary">
              {waiting} recommendation{waiting === 1 ? "" : "s"} to decide, {unread} unread alert{unread === 1 ? "" : "s"}.
            </p>
            {tickers.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {tickers.map((ticker) => <Badge key={ticker} variant="outline" className="font-mono">{ticker}</Badge>)}
              </div>
            )}
          </>
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
 * Home: the glance. What your money is worth and whether the data is fresh,
 * what needs fixing, what is waiting for a decision and what to do this month.
 * The detailed overview stays at /dashboard.
 */
export function HomePage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Home"
        subtitle="Status, and what is waiting for you."
        actions={
          <Button asChild variant="outline" size="sm" className="h-10 sm:h-8">
            <Link to="/dashboard"><PieChart className="mr-2 h-4 w-4" aria-hidden="true" /> Detailed overview</Link>
          </Button>
        }
      />
      <StatusStrip />
      <NeedsYou />
      <div className="grid gap-4 lg:grid-cols-2">
        <DecideTeaser />
        <ThisMonthCard />
      </div>
    </div>
  );
}
