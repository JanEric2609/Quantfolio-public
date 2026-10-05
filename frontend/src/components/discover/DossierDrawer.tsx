import { FileText, AlertTriangle, ArrowUp, ArrowDown, Minus, Activity, ExternalLink, Landmark } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "../../components/ui/sheet";
import { Card, CardContent } from "../../components/ui/card";
import { Badge } from "../../components/ui/badge";
import { type DiscoverDossier, type DiscoverReturnComponents } from "../../lib/api";
import { formatNumber, formatPercent, formatPercentPoints } from "../../lib/format";
import { ScoreBar } from "../ui/ScoreBar";
import { ConcernBadges } from "./ConcernBadges";
import { DossierActions } from "./DossierActions";
import { brokerAvailability } from "./brokerAvailability";

interface DossierDrawerProps {
  open: boolean;
  onClose: () => void;
  dossier: DiscoverDossier | null;
  symbol: string;
  concerns?: string[] | null;
  /** Company or fund name; omitted when it only repeats the ticker. */
  name?: string | null;
  /** The candidate's per-broker tradeability assessment (``tradeable_json``). */
  tradeable?: Record<string, unknown> | null;
}

const TRADEABILITY_BADGES: Record<string, { label: string; className: string }> = {
  high: { label: "Likely buyable", className: "bg-success/15 text-success" },
  medium: { label: "Probably buyable", className: "bg-info/15 text-info" },
  low: { label: "Probably not buyable", className: "bg-danger/15 text-danger" },
  unknown: { label: "Not checked", className: "bg-muted text-muted-foreground" },
};

function BrokerTradeability({ tradeable, symbol }: { tradeable: Record<string, unknown>; symbol: string }) {
  // The trailing "likely — confirm at …" reason is what the links are for.
  const reasons = (Array.isArray(tradeable.reasons) ? tradeable.reasons : [])
    .filter((r): r is string => typeof r === "string" && !r.startsWith("likely — confirm"));
  const brokers = brokerAvailability(tradeable);
  return (
    <section data-testid="broker-tradeability">
      <h4 className="text-sm font-semibold text-text-primary mb-2 flex items-center gap-2">
        <Landmark className="w-4 h-4 text-accent" />
        Where to buy
      </h4>
      <Card className="border-border bg-surface-2">
        <CardContent className="p-4 space-y-3 text-sm text-text-secondary">
          {reasons.map((reason, idx) => (
            <p key={idx} className="text-xs">{reason}</p>
          ))}
          <ul className="space-y-2">
            {brokers.map((b) => {
              const badge = TRADEABILITY_BADGES[b.confidence] ?? TRADEABILITY_BADGES.unknown;
              const byIsin = b.url?.includes("isin=") ?? false;
              return (
                <li key={b.key} className="space-y-1" data-testid={`broker-${b.key}`}>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold text-text-primary w-16">{b.label}</span>
                    <Badge className={`text-xs ${badge.className}`}>{badge.label}</Badge>
                    {b.url && (
                      <a
                        href={b.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
                      >
                        {byIsin ? `Check at ${b.label}` : `Search ${b.label} for ${symbol}`}
                        <ExternalLink className="w-3 h-3" />
                      </a>
                    )}
                  </div>
                  {b.note && <p className="text-xs text-text-muted">{b.note}</p>}
                  {b.key === "dkb" && b.url && !byIsin && (
                    <p className="text-xs text-text-muted">No ISIN on file — search DKB by company name or WKN.</p>
                  )}
                  {b.key === "scalable" && !b.url && (
                    <p className="text-xs text-text-muted">No ISIN on file — search the Scalable app by name.</p>
                  )}
                </li>
              );
            })}
          </ul>
        </CardContent>
      </Card>
    </section>
  );
}

// `expected_return` is a percent number on the wire (8.2 = 8.2 %), not a fraction.
function formatReturn(value: number | null): string {
  return formatPercentPoints(value, { digits: 1, signed: true });
}

function anchorKindLabel(kind: DiscoverDossier["expected_return_anchor_kind"]): string | null {
  if (kind === "trailing_3y_annualized_return") return "trailing 3-year annualized return";
  if (kind === "momentum_12_1m") return "trailing 12-1 month momentum";
  if (kind === "trailing_1m_annualized_return") return "trailing 1-month annualized return";
  if (kind === "building_blocks_credibility") return "building-blocks credibility estimate";
  if (kind === "bl_posterior") return "Black-Litterman posterior";
  return null;
}

function headlineLabel(kind: DiscoverDossier["expected_return_anchor_kind"]): string {
  if (kind === "trailing_3y_annualized_return" || kind === "trailing_1m_annualized_return") {
    return "Trailing-return estimate";
  }
  if (kind === "momentum_12_1m") return "Momentum-based estimate";
  if (kind === "building_blocks_credibility") return "Building-block estimate";
  if (kind === "bl_posterior") return "Model estimate (Black-Litterman)";
  return "Return estimate";
}

// Wire convention: signal_breakdown values are unit-less numbers except for
// the percent-unit fields listed here (see DiscoverDossier JSDoc in api.ts).
const SIGNAL_UNIT_SUFFIXES: Record<string, string> = {
  expected_return_anchor_pct: "\u00a0%",
};

function formatSignalValue(key: string, value: number | string | null): string {
  if (key === "expected_return_anchor_kind" && typeof value === "string") {
    return anchorKindLabel(value as DiscoverDossier["expected_return_anchor_kind"]) ?? value;
  }
  if (typeof value !== "number") return String(value ?? "—");
  return `${formatNumber(value, { digits: 2 })}${SIGNAL_UNIT_SUFFIXES[key] ?? ""}`;
}

const COMPOSITE_SIGNAL_LABELS: Record<string, string> = {
  ic_icir: "Factor exposure (validated factors)",
  regime: "Regime alignment",
  analyst: "Analyst consensus",
  sentiment: "News sentiment",
  portfolio: "Portfolio fit",
  fundamentals: "Fundamentals",
  momentum: "12-1m momentum (rank)",
  risk: "Risk (vol, CVaR, drawdown)",
  benchmark: "Risk-adjusted 3y vs benchmark",
  ml_signal: "ML rank (pooled model, percentile)",
  estimate_revision: "Estimate revisions",
  insider_signal: "Insider activity",
};

function regimeSourceLabel(source: unknown): string {
  if (source === "jump") return " (jump model)";
  if (source === "hmm") return " (HMM model)";
  if (source === "rule_based") return " (rule-based, as in the header)";
  return "";
}

function formatRegimeConfidence(confidence: number): string {
  // Model scores saturate near 1 (a regime switch costs many units of loss);
  // "100%" overstates certainty.
  if (confidence >= 0.995) return ">99\u00a0%";
  return formatPercent(confidence, { digits: 0 });
}

function EstimateComposition({ components }: { components: DiscoverReturnComponents }) {
  if (components.prior_pct == null || components.credibility_weight == null) return null;
  const beta = components.beta != null ? formatNumber(components.beta, { digits: 2 }) : formatNumber(1, { digits: 2 });
  return (
    <p data-testid="estimate-composition" className="text-xs text-text-muted mt-1">
      Per year: market-implied {formatPercentPoints(components.prior_pct, { digits: 1 })} (cash{" "}
      {formatPercentPoints(components.risk_free_pct, { digits: 2 })} + beta {beta} × {formatPercentPoints(components.equity_premium_pct, { digits: 1 })}{" "}
      equity premium), plus {formatPercent(components.credibility_weight, { digits: 1 })} of the gap to the
      trailing figure {formatPercentPoints(components.anchor_pct, { digits: 1 })} = {formatPercentPoints(components.er_annual_pct, { digits: 1 })}.
    </p>
  );
}

function DirectionBadge({ direction }: { direction: DiscoverDossier["direction"] }) {
  if (direction === "long") {
    return (
      <Badge className="bg-success/15 text-success flex items-center gap-1">
        <ArrowUp className="w-3 h-3" />
        LONG
      </Badge>
    );
  }
  if (direction === "short") {
    return (
      <Badge className="bg-danger/15 text-danger flex items-center gap-1">
        <ArrowDown className="w-3 h-3" />
        SHORT
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="flex items-center gap-1">
      <Minus className="w-3 h-3" />
      NEUTRAL
    </Badge>
  );
}

export function DossierDrawer({ open, onClose, dossier, symbol, concerns, name, tradeable }: DossierDrawerProps) {
  const displayName = name && name !== symbol ? name : null;
  const hasTradeability = tradeable != null && Object.keys(tradeable).length > 0;
  // Wire units differ: expected_return is a percent number, band members are
  // fractions — hence the ×100 before comparing (>5pp divergence flags O2).
  const bandP50DivergesFromHeadline =
    dossier != null &&
    dossier.expected_return != null &&
    dossier.expected_return_band != null &&
    Math.abs(dossier.expected_return_band.p50 * 100 - dossier.expected_return) > 5;
  return (
    <Sheet open={open} onOpenChange={(v) => !v && onClose()}>
      <SheetContent side="right" className="w-full sm:max-w-lg overflow-y-auto">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <FileText className="w-5 h-5 text-accent" />
            Dossier: {symbol}
          </SheetTitle>
          <SheetDescription>
            {displayName ? `${displayName} · ` : ""}
            {dossier ? "AI-generated research dossier" : "Loading dossier..."}
          </SheetDescription>
        </SheetHeader>

        {!dossier ? (
          <div className="py-8 text-center text-sm text-text-muted">Loading...</div>
        ) : (
          <div className="space-y-5 mt-4">
            {/* Header row: symbol + direction */}
            <div className="flex items-center justify-between">
              <span className="text-lg font-semibold text-text-primary">{symbol}</span>
              <div className="flex items-center gap-2">
                {dossier.generated_by === "fallback" && (
                  <Badge
                    variant="secondary"
                    title={dossier.fallback_reason ?? undefined}
                    className="bg-warn/15 text-warn"
                  >
                    Quant template — LLM unavailable
                  </Badge>
                )}
                <DirectionBadge direction={dossier.direction} />
              </div>
            </div>

            {/* Conviction gauge */}
            <section>
              <h4 className="text-sm font-semibold text-text-primary mb-2">Signal score</h4>
              <ScoreBar label="Score (0–1, not a probability)" value={dossier.conviction} hint="Composite ranking score from 0 to 1, built from trailing signals. Not a probability that the pick beats the market." />
            </section>

            {/* Return estimate */}
            <section>
              <h4 className="text-sm font-semibold text-text-primary mb-2">
                {headlineLabel(dossier.expected_return_anchor_kind)}{" "}
                <span className="font-normal text-text-muted">({dossier.horizon_months}mo horizon)</span>
              </h4>
              <div className="flex items-center gap-3">
                <span className="text-2xl font-bold text-text-primary">
                  {formatReturn(dossier.expected_return)}
                </span>
              </div>
              {dossier.expected_return_band && (
                <div data-testid="return-band" className="mt-1.5 space-y-0.5">
                  <p className="text-xs font-mono text-text-muted">
                    P10 {formatPercent(dossier.expected_return_band.p10, { decimals: 1 })} · P50{" "}
                    {formatPercent(dossier.expected_return_band.p50, { decimals: 1 })} · P90{" "}
                    {formatPercent(dossier.expected_return_band.p90, { decimals: 1 })}
                  </p>
                  {bandP50DivergesFromHeadline && (
                    <p data-testid="band-anchor-note" className="text-xs text-text-muted">
                      Range simulated around the undampened historical anchor — its median
                      intentionally differs from our shrunk estimate above.
                    </p>
                  )}
                  <p className="text-xs text-text-muted">
                    Simulated range over the stated horizon (P10–P90); estimates, not advice.
                    The headline above is our shrunk estimate — where the range's median differs
                    from it, see the note below.
                    {dossier.expected_return_band.method === "gbm_fallback" &&
                      " (widened — short history)"}
                  </p>
                </div>
              )}
              {dossier.expected_return_components && (
                <EstimateComposition components={dossier.expected_return_components} />
              )}
              {anchorKindLabel(dossier.expected_return_anchor_kind) && (
                <p className="text-xs text-text-muted mt-1">
                  Anchored on {anchorKindLabel(dossier.expected_return_anchor_kind)} — a historical figure, not a
                  guarantee of future performance.
                </p>
              )}
            </section>

            {/* Thesis */}
            <section>
              <h4 className="text-sm font-semibold text-text-primary mb-2">Thesis</h4>
              <Card className="border-border bg-surface-2">
                <CardContent className="p-4">
                  <blockquote className="text-sm text-text-secondary leading-relaxed italic border-l-2 border-accent pl-3">
                    {dossier.thesis}
                  </blockquote>
                </CardContent>
              </Card>
            </section>

            {/* Key Risks */}
            {dossier.key_risks.length > 0 && (
              <section>
                <h4 className="text-sm font-semibold text-text-primary mb-2 flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4 text-warn" />
                  Key Risks
                </h4>
                <ul className="space-y-1">
                  {dossier.key_risks.map((risk, idx) => (
                    <li key={idx} className="flex items-start gap-2 text-sm text-text-secondary">
                      <span className="text-text-muted mt-1">•</span>
                      <span>{risk}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {/* Data caveats surfaced from candidate scoring */}
            {(concerns?.length ?? 0) > 0 && (
              <section>
                <h4 className="text-sm font-semibold text-text-primary mb-2 flex items-center gap-2">
                  <AlertTriangle className="w-4 h-4 text-warn" />
                  Data Caveats
                </h4>
                <ConcernBadges concerns={concerns} />
              </section>
            )}

            <section data-testid="dossier-actions" aria-label="Dossier actions">
              <DossierActions symbol={symbol} name={displayName} horizonMonths={dossier.horizon_months} />
            </section>

            {hasTradeability && <BrokerTradeability tradeable={tradeable} symbol={symbol} />}

            {/* Market Regime context — substitute signal for passive ETFs */}
            {dossier.regime_context && (
              <section>
                <h4 className="text-sm font-semibold text-text-primary mb-2 flex items-center gap-2">
                  <Activity className="w-4 h-4 text-accent" />
                  Market Regime
                </h4>
                <Card className="border-border bg-surface-2">
                  <CardContent className="p-4 space-y-1 text-sm text-text-secondary">
                    <p>
                      Current regime{regimeSourceLabel(dossier.regime_context.source)}:{" "}
                      <span className="font-medium text-text-primary">
                        {(dossier.regime_context.regime_label ?? "unknown").replace(/_/g, " ")}
                      </span>
                    </p>
                    <p>
                      Risk-on:{" "}
                      <span className="font-medium text-text-primary">
                        {dossier.regime_context.risk_on ? "yes" : "no"}
                      </span>
                    </p>
                    {dossier.regime_context.crisis && (
                      <p className="text-warn">Crisis conditions active — discovery paused</p>
                    )}
                    {dossier.regime_context.confidence != null && (
                      <p>
                        Snapshot confidence:{" "}
                        <span className="font-medium text-text-primary">
                          {formatRegimeConfidence(dossier.regime_context.confidence)}
                        </span>
                      </p>
                    )}
                    {dossier.regime_context.reason && (
                      <p className="text-xs text-text-muted">{dossier.regime_context.reason}</p>
                    )}
                    {(dossier.regime_context.source === "jump" || dossier.regime_context.source === "hmm") && (
                      <p className="text-xs text-text-muted">
                        The header chip shows a separate rule-based regime (VIX, yield curve, momentum); the two
                        can disagree.
                      </p>
                    )}
                  </CardContent>
                </Card>
              </section>
            )}

            {/* What drives the score: every composite input with its weight */}
            {(dossier.composite_breakdown?.length ?? 0) > 0 && (
              <section>
                <h4 className="text-sm font-semibold text-text-primary mb-2">What drives the score</h4>
                <Card className="border-border bg-surface-2">
                  <CardContent className="p-0">
                    <table data-testid="composite-breakdown" className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-border text-xs text-text-muted">
                          <th className="px-4 py-2 text-left font-normal">Signal</th>
                          <th className="px-2 py-2 text-right font-normal">Score</th>
                          <th className="px-2 py-2 text-right font-normal">Weight</th>
                          <th className="px-4 py-2 text-right font-normal">Adds</th>
                        </tr>
                      </thead>
                      <tbody>
                        {dossier.composite_breakdown!.map((row) => (
                          <tr key={row.signal} className="border-b border-border last:border-b-0">
                            <td className="px-4 py-2 text-text-secondary">
                              {COMPOSITE_SIGNAL_LABELS[row.signal] ?? row.signal.replace(/_/g, " ")}
                            </td>
                            <td className="px-2 py-2 text-right text-text-secondary">{formatNumber(row.score, { digits: 2 })}</td>
                            <td className="px-2 py-2 text-right text-text-secondary">
                              {formatPercent(row.weight, { digits: 0 })}
                            </td>
                            <td className="px-4 py-2 text-right font-medium text-text-primary">
                              {formatNumber(row.contribution, { digits: 3 })}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </CardContent>
                </Card>
                <p className="text-xs text-text-muted mt-1">
                  The contributions add up to the score before any repeat-suggestion penalty.
                </p>
              </section>
            )}

            {/* Signal Breakdown */}
            {Object.keys(dossier.signal_breakdown).length > 0 && (
              <section>
                <h4 className="text-sm font-semibold text-text-primary mb-2">Signal Breakdown</h4>
                <Card className="border-border bg-surface-2">
                  <CardContent className="p-0">
                    <table className="w-full text-sm">
                      <tbody>
                        {Object.entries(dossier.signal_breakdown).map(([key, value]) => (
                          <tr key={key} className="border-b border-border last:border-b-0">
                            <td className="px-4 py-2 text-text-secondary capitalize">
                              {key.replace(/_/g, " ")}
                            </td>
                            <td className="px-4 py-2 text-right font-medium text-text-primary">
                              {formatSignalValue(key, value)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </CardContent>
                </Card>
              </section>
            )}

            {/* Disclaimer */}
            {dossier.estimate && dossier.not_financial_advice && (
              <Card className="border-warn/30 bg-warn/10">
                <CardContent className="p-4 text-sm text-text-secondary">
                  <p className="font-medium text-warn mb-1">Disclaimer</p>
                  <p>
                    This is an estimate and does not constitute financial advice. Broker statements and
                    independent professional advice remain the source of truth.
                  </p>
                </CardContent>
              </Card>
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
