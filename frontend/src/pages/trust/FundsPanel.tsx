import { Layers } from "lucide-react";
import type { TrustFunds, TrustHeldFund } from "../../lib/api";
import { Badge } from "../../components/ui/badge";
import { Skeleton } from "../../components/ui/skeleton";
import { fmtDay } from "./trustFormat";
import { useTrustFunds } from "./useTrust";

/** Signed % a year with two decimals: tracking differences are a few hundredths of a percent. */
function fmtTd(v: number): string {
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)} % a year`;
}

function FundRow({ f }: { f: TrustHeldFund }) {
  return (
    <div role="listitem" className="flex flex-wrap items-start justify-between gap-2 text-xs" data-testid={`fund-${f.isin}`}>
      <div className="min-w-0 space-y-0.5">
        <p className="font-medium text-text-primary">{f.name}</p>
        <p className="text-text-secondary">
          {f.td_pct == null
            ? `${f.index}: no measured peer on the same index.`
            : `${f.index}: ${fmtTd(f.td_pct)} against the median of its peers.`}
          {f.best_peer && f.gap_pct != null
            ? ` ${f.best_peer.name} tracked ${f.gap_pct.toFixed(2)} % a year better.`
            : ""}
        </p>
      </div>
      {f.td_pct != null ? (
        <Badge variant={f.flag ? "warning" : "success"} className="w-fit text-xs">
          {f.flag ? "Trails a peer" : "Tracks well"}
        </Badge>
      ) : null}
    </div>
  );
}

function Panel({ data }: { data: TrustFunds }) {
  const funds = data.funds ?? [];
  if (funds.length === 0) {
    return (
      <p className="text-sm text-text-secondary" role="status">
        None of your synced positions is a fund from the curated plan list, so there is nothing to compare.
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <div role="list" className="space-y-2">
        {funds.map((f) => (
          <FundRow key={f.isin} f={f} />
        ))}
      </div>
      <p className="text-xs text-text-muted">
        {data.as_of ? `Measured ${fmtDay(data.as_of)} over ${data.years} years. ` : "No current measurement: the figures come with the monthly refresh. "}
        {`A fund more than ${data.flag_pct} % a year behind its best same-index peer is flagged. `}
        {data.n_other_positions > 0 ? `${data.n_other_positions} other positions are not compared. ` : ""}
        {data.note}
      </p>
    </div>
  );
}

/** "Funds": each held curated fund against the funds on the same index (plan 2026-10-14, C4 live check). */
export function FundsPanel() {
  const query = useTrustFunds();
  return (
    <section aria-labelledby="trust-funds" className="space-y-3 rounded-lg border border-border p-4">
      <h2 id="trust-funds" className="flex items-center gap-2 text-sm font-semibold text-text-primary">
        <Layers className="h-4 w-4 text-text-muted" aria-hidden="true" />
        Funds: do yours track their index well?
      </h2>
      {query.isLoading ? (
        <Skeleton className="h-16 w-full rounded-md" />
      ) : query.isError || !query.data ? (
        <p className="text-sm text-text-secondary" role="status">
          The fund comparison is unavailable right now.
        </p>
      ) : (
        <Panel data={query.data} />
      )}
    </section>
  );
}
