import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, XCircle } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Badge } from "../ui/badge";
import { Skeleton } from "../ui/skeleton";
import { formatDateTime, formatNumber, formatPercent } from "../../lib/format";
import { getFactorPremia, type FactorEvidenceCard } from "../../lib/api";

function pa(value: number | null): string {
  return formatPercent(value, { digits: 1, signed: true });
}

const RUN_COMMAND = "cd /opt/quantfolio/backend && set -a && . ../.env && set +a && .venv/bin/python -m app.lab.factor_premia";

function Row({ card }: { card: FactorEvidenceCard }) {
  const failed = card.checks.filter((c) => !c.passed).map((c) => c.label);
  return (
    <tr className="border-b border-border align-top last:border-0">
      <td className="py-2 pr-3">
        <div className="flex items-center gap-1.5 font-medium text-text-primary">
          {card.passed ? (
            <CheckCircle2 className="h-4 w-4 shrink-0 text-success" />
          ) : (
            <XCircle className="h-4 w-4 shrink-0 text-text-secondary" />
          )}
          {card.label}
        </div>
        <div className="text-xs text-text-muted">{card.citation}</div>
        {!card.passed && failed.length > 0 && (
          <div className="mt-1 text-xs text-text-secondary">Failed: {failed.join("; ")}</div>
        )}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums">{Math.floor(card.months / 12)}</td>
      <td className="py-2 pr-3 text-right tabular-nums">
        {pa(card.long_short_annual)}
        <div className="text-xs text-text-muted">t {card.long_short_t == null ? "—" : formatNumber(card.long_short_t, { digits: 1 })}</div>
      </td>
      <td className="py-2 pr-3 text-right tabular-nums">{pa(card.post_publication_annual)}</td>
      <td className="py-2 pr-3 text-right tabular-nums font-medium">{pa(card.net_expected_annual)}</td>
      <td className="py-2 text-right tabular-nums">{pa(card.book_worst_5y_lag)}</td>
    </tr>
  );
}

// Only this region's cards can unlock the tilt (backend TILT_EVIDENCE_REGION):
// the tilt is a world factor ETF held next to an MSCI World core.
const GATING_REGION = "world";

const REGION_LABEL: Record<string, string> = {
  world: "World (23 MSCI World markets)",
  europe: "Europe (15 MSCI Europe markets)",
};

function RegionTable({ cards }: { cards: FactorEvidenceCard[] }) {
  const first = cards[0];
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-text-secondary">
        <Badge variant={first.region === GATING_REGION ? "default" : "secondary"}>
          {REGION_LABEL[first.region] ?? first.region}
        </Badge>
        <span>{first.region === GATING_REGION ? "Gates the tilt" : "For comparison only"}</span>
        <span>
          · {first.start} to {first.end}
          {first.computed_at ? ` · computed ${formatDateTime(first.computed_at)}` : ""}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px] text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-text-secondary">
              <th className="py-2 pr-3 font-medium">Strategy</th>
              <th className="py-2 pr-3 text-right font-medium">Years</th>
              <th className="py-2 pr-3 text-right font-medium">Long-short p.a.</th>
              <th className="py-2 pr-3 text-right font-medium">After publication</th>
              <th className="py-2 pr-3 text-right font-medium" title="Long-only over the market, after decay, costs and tax">
                Expected, net
              </th>
              <th className="py-2 text-right font-medium" title={`Worst 5-year excess at a ${first.tilt_cap_pct} % tilt`}>
                Worst 5y, book
              </th>
            </tr>
          </thead>
          <tbody>
            {cards.map((c) => (
              <Row key={c.strategy} card={c} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** The evidence that decides whether the monthly plan's factor tilt may receive money. */
export function FactorPremiaCard() {
  const { data, isLoading, error } = useQuery({ queryKey: ["factor-premia"], queryFn: getFactorPremia });

  // The API lists each region's latest run, gating region first.
  const regions: FactorEvidenceCard[][] = [];
  for (const card of data ?? []) {
    const group = regions.find((g) => g[0].region === card.region);
    if (group) group.push(card);
    else regions.push([card]);
  }
  const worldMissing = regions.length > 0 && !regions.some((g) => g[0].region === GATING_REGION);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Factor premia: the tilt's evidence</CardTitle>
        <CardDescription>
          Pre-registered value, momentum, quality-style, low-volatility and size portfolios on the JKP data (top vs bottom third, within
          country and month). A strategy passes with at least 20 years of data, a Newey-West t ≥ 2, a positive return
          after its publication year, and a positive long-only return over the market after a 58 % decay haircut, factor
          ETF costs and German tax. Only a pass on the world markets can unlock the tilt on "This month", because the
          tilt would be a world factor ETF.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : error ? (
          <p className="text-sm text-danger">{error instanceof Error ? error.message : "Failed to load"}</p>
        ) : regions.length === 0 ? (
          <div className="space-y-2 text-sm text-text-secondary">
            <p>The study has not been run yet. On the app LXC:</p>
            <pre className="overflow-x-auto rounded-md bg-surface-2 p-2 text-xs">{RUN_COMMAND}</pre>
          </div>
        ) : (
          <div className="space-y-5">
            {worldMissing && (
              <div className="space-y-2 text-sm text-text-secondary">
                <p>No world run yet, so the tilt stays locked. On the app LXC:</p>
                <pre className="overflow-x-auto rounded-md bg-surface-2 p-2 text-xs">{RUN_COMMAND}</pre>
              </div>
            )}
            {regions.map((cards) => (
              <RegionTable key={cards[0].region} cards={cards} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
