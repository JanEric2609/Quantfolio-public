import { FileText, Clock, ShieldAlert } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";

import { type DiscoverCandidate, parseConcerns } from "../../lib/api";
import { formatPercentPoints } from "../../lib/format";
import { ScoreBar } from "../ui/ScoreBar";
import { ConcernBadges } from "./ConcernBadges";
import { brokerAvailability } from "./brokerAvailability";
import { DossierActions } from "./DossierActions";

interface ShortlistCardsProps {
  candidates: DiscoverCandidate[];
  onOpenDossier: (candidate: DiscoverCandidate) => void;
}

const SOURCE_LABELS: Record<string, string> = {
  screen_index: "Index screen",
  screen_etf: "ETF screen",
  news_sentiment: "In the news",
};

export function sourceLabel(source: string | null | undefined): string {
  if (!source) return "";
  return SOURCE_LABELS[source] ?? source.replace(/_/g, " ");
}

/** The score the card shows: the dossier's conviction, else the composite. */
function cardScore(candidate: DiscoverCandidate): number {
  return candidate.conviction != null ? candidate.conviction : (candidate.scores?.composite ?? 0);
}

// `expected_return` is a percent number on the wire (8.2 = 8.2 %), not a fraction.
function formatReturn(value: number | null | undefined): string {
  return formatPercentPoints(value, { digits: 1, signed: true });
}

function returnChipClass(value: number | null | undefined): string {
  if (value == null) return "bg-muted text-muted-foreground";
  if (value > 0) return "bg-success/15 text-success";
  if (value < 0) return "bg-danger/15 text-danger";
  return "bg-muted text-muted-foreground";
}

/** "DKB ✓ · Scalable ✓": where this pick is likely buyable; details and links are in the dossier. */
function BrokerBadges({ tradeable }: { tradeable: Record<string, unknown> | null | undefined }) {
  const brokers = brokerAvailability(tradeable);
  if (brokers.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5" aria-label="Where to buy">
      {brokers.map((b) => (
        <Badge
          key={b.key}
          variant={b.likely ? "success" : b.likely === false ? "secondary" : "outline"}
          className="text-xs"
          title={b.likely ? `Likely buyable at ${b.label}` : b.likely === false ? `Probably not at ${b.label}` : `Not checked at ${b.label}`}
        >
          {b.label} {b.likely ? "✓" : b.likely === false ? "✗" : "?"}
        </Badge>
      ))}
    </div>
  );
}

export function ShortlistCards({ candidates, onOpenDossier }: ShortlistCardsProps) {
  // Best first: the API returns a run's candidates by symbol, which put
  // AMD ahead of every stronger name.
  const shortlisted = candidates
    .filter((c) => c.status === "shortlisted")
    .sort((a, b) => cardScore(b) - cardScore(a));

  if (shortlisted.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Shortlist</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-text-muted py-4 text-center">No shortlisted candidates yet.</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold text-text-primary uppercase tracking-wider">Shortlist</h3>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {shortlisted.map((candidate) => {
          const conviction = cardScore(candidate);
          // Runs before 2026-09-28 stored the ticker as the name.
          const name = candidate.name && candidate.name !== candidate.symbol ? candidate.name : null;
          const origin = sourceLabel(candidate.source);
          const horizon = candidate.horizon_months;
          const expectedReturn = candidate.expected_return;

          return (
            <Card
              key={candidate.id}
              className="cursor-pointer hover:border-accent/50 transition-colors"
              onClick={() => onOpenDossier(candidate)}
            >
              <CardHeader className="pb-2">
                <div className="flex items-center justify-between">
                  <div>
                    <CardTitle className="text-base">{candidate.symbol}</CardTitle>
                    <div className="text-xs text-text-muted">
                      {[name, origin].filter(Boolean).join(" · ") || "—"}
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={(e) => {
                      e.stopPropagation();
                      onOpenDossier(candidate);
                    }}
                    aria-label="Open dossier"
                  >
                    <FileText className="w-4 h-4" />
                  </Button>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <ScoreBar label="Signal score" value={conviction} hint="Composite ranking score from 0 to 1, built from trailing signals. Not a probability that the pick beats the market." />

                <div className="flex items-center gap-3">
                  <Badge variant="secondary" className="flex items-center gap-1 text-xs">
                    <Clock className="w-3 h-3" />
                    {horizon != null ? `${horizon}mo` : "--"}
                  </Badge>

                  <div className="flex flex-col">
                    <Badge className={`text-xs ${returnChipClass(expectedReturn)}`}>
                      Ret. est. {formatReturn(expectedReturn)}
                    </Badge>
                  </div>
                </div>

                {candidate.withheld && (
                  <Badge variant="warning" className="flex items-center gap-1 text-xs w-fit">
                    <ShieldAlert className="w-3 h-3" />
                    Withheld — cohort track record unproven
                  </Badge>
                )}

                <BrokerBadges tradeable={candidate.tradeable} />

                <ConcernBadges concerns={parseConcerns(candidate.scores)} />

                <DossierActions symbol={candidate.symbol} name={name} horizonMonths={horizon} />
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
