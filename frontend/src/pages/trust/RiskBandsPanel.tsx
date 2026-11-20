import { ShieldAlert } from "lucide-react";
import type { TrustRiskBandHorizon, TrustRiskBands } from "../../lib/api";
import { Badge } from "../../components/ui/badge";
import { Skeleton } from "../../components/ui/skeleton";
import { fmtDay, fmtPct } from "./trustFormat";
import { useTrustRiskBands } from "./useTrust";

/** 1 / 0.05: an e-value this large rejects "the bands are right" at 5 %. */
const REJECT_AT = 20;

function verdict(h: TrustRiskBandHorizon): { label: string; variant: "secondary" | "success" | "danger" } {
  if (h.e_var >= REJECT_AT || h.e_es >= REJECT_AT) return { label: "Bands too narrow", variant: "danger" };
  if (h.n_scored < 20) return { label: "Too early", variant: "secondary" };
  return { label: "No sign of trouble", variant: "success" };
}

function HorizonRow({ h }: { h: TrustRiskBandHorizon }) {
  const v = verdict(h);
  const days = h.horizon_days === 1 ? "1 day" : `${h.horizon_days} days`;
  return (
    <div role="listitem" className="grid grid-cols-[4.5rem_1fr] gap-x-3 gap-y-1 text-xs sm:grid-cols-[4.5rem_1fr_auto]">
      <span className="font-medium text-text-primary">{days}</span>
      <span className="text-text-secondary">
        Typical move {fmtPct(h.sigma, 1)}; a loss beyond {fmtPct(h.var95, 1)} on about 1 in 20; in the worst 2.5 % an
        average loss of {fmtPct(h.es975, 1)}.{" "}
        <span className="text-text-muted">
          Scored {h.n_scored}, beyond the band {h.breaches}
          {h.n_scored > 0 && ` (${fmtPct(h.breaches / h.n_scored, 0)}, expected 5 %)`}.
          {(h.unpriced_share ?? 0) > 0.01 && ` ${fmtPct(h.unpriced_share, 0)} of the value has no stored prices and counts as flat.`}
        </span>
      </span>
      <Badge variant={v.variant} className="w-fit text-xs">
        {v.label}
      </Badge>
    </div>
  );
}

function Panel({ data }: { data: TrustRiskBands }) {
  const targets = data.targets ?? [];
  if (targets.length === 0) {
    return (
      <p className="text-sm text-text-secondary" role="status">
        No risk forecast yet: the first is made after tonight's close.
      </p>
    );
  }
  return (
    <div className="space-y-3">
      {targets.map((t) => (
        <div key={t.target} className="space-y-1" data-testid={`risk-band-${t.target}`}>
          <h3 className="text-sm font-semibold text-text-primary">
            {t.label}
            {t.horizons[0] && (
              <span className="ml-2 text-[11px] font-normal text-text-muted">as of {fmtDay(t.horizons[0].forecast_day)}</span>
            )}
          </h3>
          <div role="list" className="space-y-1">
            {t.horizons.map((h) => (
              <HorizonRow key={h.horizon_days} h={h} />
            ))}
          </div>
        </div>
      ))}
      {data.note && <p className="text-xs text-text-muted">{data.note}</p>}
    </div>
  );
}

/** "Risk bands": forecasts stored before the fact, scored once their horizon has passed (plan D3). */
export function RiskBandsPanel() {
  const query = useTrustRiskBands();
  return (
    <section aria-labelledby="trust-risk-bands" className="space-y-3 rounded-lg border border-border p-4">
      <h2 id="trust-risk-bands" className="flex items-center gap-2 text-sm font-semibold text-text-primary">
        <ShieldAlert className="h-4 w-4 text-text-muted" aria-hidden="true" />
        Risk bands (forecast before the fact)
      </h2>
      {query.isLoading ? (
        <Skeleton className="h-24 w-full rounded-md" />
      ) : query.isError || !query.data ? (
        <p className="text-sm text-text-secondary" role="status">
          The risk bands are unavailable right now.
        </p>
      ) : (
        <Panel data={query.data} />
      )}
    </section>
  );
}
