import { useSearchParams } from "react-router-dom";
import { Check, Lock, ListChecks, X } from "lucide-react";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Card } from "../../components/ui/card";
import { Skeleton } from "../../components/ui/skeleton";
import { EmptyState } from "../../components/composed/EmptyState";
import { ErrorState } from "../../components/composed/ErrorState";
import type { TrustCall, TrustTypeKey } from "../../lib/api";
import { fmtDay, fmtPct, fmtPp, outcomeText } from "./trustFormat";
import { useTrustCalls } from "./useTrust";

const PAGE_SIZE = 25;
const FILTERS: { key: TrustTypeKey | null; label: string }[] = [
  { key: null, label: "All" },
  { key: "ideas", label: "Ideas" },
  { key: "advisor", label: "Advisor" },
  { key: "mandates", label: "Mandates" },
];

function statedText(call: TrustCall): string {
  const parts: string[] = [];
  // A mandate's confidence is the model's own number, not a calibrated probability.
  if (call.stated_p != null) parts.push(call.type === "mandates" ? `${fmtPct(call.stated_p)} (model's own)` : fmtPct(call.stated_p));
  if (call.stated_range) parts.push(`${fmtPp(call.stated_range[0], 0)} to ${fmtPp(call.stated_range[1], 0)}`);
  return parts.length ? parts.join(" · ") : "—";
}

function HitMark({ hit }: { hit: boolean }) {
  return hit ? (
    <span className="inline-flex items-center gap-1 text-success">
      <Check className="h-3.5 w-3.5" aria-hidden="true" />
      Hit
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-danger">
      <X className="h-3.5 w-3.5" aria-hidden="true" />
      Miss
    </span>
  );
}

function CallCards({ calls }: { calls: TrustCall[] }) {
  return (
    <ul className="space-y-2 sm:hidden" aria-label="Resolved calls">
      {calls.map((c) => (
        <li key={`${c.type}-${c.id}`}>
          <Card className="space-y-1 p-3 text-sm">
            <div className="flex items-center justify-between gap-2">
              <span className="font-semibold text-text-primary">{c.subject}</span>
              <HitMark hit={c.hit} />
            </div>
            <p className="text-xs text-text-secondary">{c.call}</p>
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
              <dt className="text-text-muted">Stated</dt>
              <dd className="text-right font-mono tabular-nums">{statedText(c)}</dd>
              <dt className="text-text-muted">Outcome</dt>
              <dd className="text-right font-mono tabular-nums">{outcomeText(c)}</dd>
              <dt className="text-text-muted">Your ETF</dt>
              <dd className="text-right font-mono tabular-nums">{c.benchmark_outcome != null ? fmtPp(c.benchmark_outcome) : "—"}</dd>
              <dt className="text-text-muted">Excess</dt>
              <dd className="text-right font-mono tabular-nums">{c.excess != null ? fmtPp(c.excess) : "—"}</dd>
            </dl>
            <p className="text-[11px] text-text-muted">Resolved {fmtDay(c.resolved_at)}</p>
          </Card>
        </li>
      ))}
    </ul>
  );
}

function CallsTable({ calls }: { calls: TrustCall[] }) {
  return (
    <div className="hidden overflow-x-auto rounded-md border border-border sm:block">
      <table className="w-full text-sm">
        <caption className="sr-only">Resolved calls, newest first</caption>
        <thead>
          <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
            <th scope="col" className="px-3 py-2">Resolved</th>
            <th scope="col" className="px-3 py-2">Call</th>
            <th scope="col" className="px-3 py-2">Stated</th>
            <th scope="col" className="px-3 py-2 text-right">Outcome</th>
            <th scope="col" className="px-3 py-2 text-right">Your ETF</th>
            <th scope="col" className="px-3 py-2 text-right">Excess</th>
            <th scope="col" className="px-3 py-2">Result</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((c) => (
            <tr key={`${c.type}-${c.id}`} className="border-b border-border/60 last:border-0">
              <td className="whitespace-nowrap px-3 py-2 text-text-secondary">{fmtDay(c.resolved_at)}</td>
              <td className="px-3 py-2">
                <span className="font-semibold text-text-primary">{c.subject}</span>
                <span className="block text-xs text-text-secondary">{c.call}</span>
              </td>
              <td className="whitespace-nowrap px-3 py-2 font-mono text-xs tabular-nums">{statedText(c)}</td>
              <td className="px-3 py-2 text-right font-mono tabular-nums">{outcomeText(c)}</td>
              <td className="px-3 py-2 text-right font-mono tabular-nums">
                {c.benchmark_outcome != null ? fmtPp(c.benchmark_outcome) : "—"}
              </td>
              <td className="px-3 py-2 text-right font-mono tabular-nums">{c.excess != null ? fmtPp(c.excess) : "—"}</td>
              <td className="px-3 py-2">
                <HitMark hit={c.hit} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function WorstMisses({ calls }: { calls: TrustCall[] }) {
  if (calls.length === 0) return null;
  return (
    <section aria-labelledby="worst-misses" className="space-y-2">
      <h2 id="worst-misses" className="text-sm font-semibold text-text-primary">
        Worst misses
      </h2>
      <p className="text-xs text-text-secondary">The calls that did worst against simply holding your ETF.</p>
      <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {calls.map((c) => (
          <li key={`${c.type}-${c.id}`}>
            <Card className="space-y-0.5 border-danger/40 p-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold text-text-primary">{c.subject}</span>
                <span className="font-mono tabular-nums text-danger">{fmtPp(c.excess)}</span>
              </div>
              <p className="text-xs text-text-secondary">
                {c.call} · resolved {fmtDay(c.resolved_at)}
              </p>
            </Card>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function CallsTab() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("type");
  const type = (FILTERS.find((f) => f.key === raw)?.key ?? null) as TrustTypeKey | null;
  const page = Math.max(0, Number(params.get("page") ?? 0) || 0);
  const query = useTrustCalls(type, PAGE_SIZE, page * PAGE_SIZE);

  const setFilter = (key: TrustTypeKey | null) => {
    const next = new URLSearchParams(params);
    if (key) next.set("type", key);
    else next.delete("type");
    next.delete("page");
    setParams(next, { replace: true });
  };
  const setPage = (p: number) => {
    const next = new URLSearchParams(params);
    if (p > 0) next.set("page", String(p));
    else next.delete("page");
    setParams(next, { replace: true });
  };

  const data = query.data;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div role="group" aria-label="Prediction type" className="flex flex-wrap items-center gap-1">
          {FILTERS.map((f) => (
            <Button
              key={f.label}
              type="button"
              size="sm"
              variant={type === f.key ? "default" : "outline"}
              aria-pressed={type === f.key}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </Button>
          ))}
        </div>
        <Badge variant="outline" className="gap-1.5" title="Calls are stored when made and never rewritten.">
          <Lock className="h-3 w-3" aria-hidden="true" />
          Frozen at issue time
        </Badge>
      </div>

      {query.isLoading ? (
        <div className="space-y-2" aria-busy="true" aria-label="Loading resolved calls">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-12 w-full rounded-md" />
          ))}
        </div>
      ) : query.isError || !data ? (
        <ErrorState
          title="Couldn't load the resolved calls"
          body={query.error instanceof Error ? query.error.message : undefined}
          onRetry={() => query.refetch()}
        />
      ) : data.total === 0 ? (
        <EmptyState
          icon={ListChecks}
          title="No resolved calls yet"
          description={
            "Calls appear here once their horizon has passed and they have been scored against your ETF. " +
            `Discover ideas and advisor trades resolve after ${data?.horizon_days ? `${data.horizon_days} trading days` : "their horizon"}.`
          }
        />
      ) : (
        <>
          {page === 0 ? <WorstMisses calls={data.worst_misses} /> : null}
          <section aria-labelledby="all-calls" className="space-y-2">
            <h2 id="all-calls" className="text-sm font-semibold text-text-primary">
              Resolved calls ({data.total})
            </h2>
            <CallCards calls={data.items} />
            <CallsTable calls={data.items} />
            <div className="flex items-center justify-between text-xs text-text-secondary">
              <span>
                {data.offset + 1} to {data.offset + data.items.length} of {data.total}
              </span>
              <div className="flex gap-2">
                <Button type="button" size="sm" variant="outline" disabled={page === 0} onClick={() => setPage(page - 1)}>
                  Newer
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={data.offset + data.items.length >= data.total}
                  onClick={() => setPage(page + 1)}
                >
                  Older
                </Button>
              </div>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
