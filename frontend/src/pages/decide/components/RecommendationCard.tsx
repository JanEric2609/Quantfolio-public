import { Check, Clock, ExternalLink, X } from "lucide-react";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { formatPercent } from "../../../lib/format";
import { timeAgo } from "../../../lib/relativeTime";
import type { AdvisorFeedbackAction, PendingRecommendation } from "../../../lib/api";

/**
 * One recommendation waiting for a decision. Accept / Reject / Snooze sit in a
 * row of big, equal buttons at the bottom of the card, where a thumb rests.
 */
export function RecommendationCard({ rec, busy, onAction }: {
  rec: PendingRecommendation;
  /** Disables the buttons while a decision on this card is in flight. */
  busy: boolean;
  onAction: (action: AdvisorFeedbackAction) => void;
}) {
  const label = rec.ticker ?? rec.name ?? "recommendation";
  return (
    <article className="space-y-3 rounded-lg border border-border bg-surface p-4" aria-label={`Recommendation ${label}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[1rem] font-semibold text-text-primary">{rec.ticker ?? "—"}</span>
            <Badge variant="outline">{rec.verdict}</Badge>
            {rec.resurfaced && <Badge variant="secondary">Snooze ended</Badge>}
          </div>
          {rec.name && <div className="truncate text-sm text-text-secondary">{rec.name}</div>}
        </div>
      </div>

      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-text-secondary">
        <span>Confidence {formatPercent(rec.confidence, { decimals: 0 })}</span>
        {rec.horizon && <span>Horizon {rec.horizon}</span>}
        {rec.created_at && <span>{timeAgo(rec.created_at)}</span>}
      </div>

      {rec.summary && <p className="line-clamp-4 text-sm text-text-primary">{rec.summary}</p>}

      {(rec.links?.length ?? 0) > 0 && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <span className="text-text-muted">{rec.side === "sell" ? "Sell" : "Buy"} it yourself at</span>
          {rec.links!.map((link) => (
            <a
              key={link.broker}
              href={link.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-accent hover:underline"
            >
              {link.label} <ExternalLink className="h-3 w-3" aria-hidden="true" />
            </a>
          ))}
        </div>
      )}

      <div className="grid grid-cols-3 gap-2">
        <Button
          type="button"
          variant="outline"
          className="h-12 gap-1.5 px-2"
          disabled={busy}
          aria-label={`Reject ${label}`}
          onClick={() => onAction("rejected")}
        >
          <X className="h-4 w-4 shrink-0" aria-hidden="true" /> Reject
        </Button>
        <Button
          type="button"
          variant="outline"
          className="h-12 gap-1.5 px-2"
          disabled={busy}
          aria-label={`Snooze ${label}`}
          onClick={() => onAction("snoozed")}
        >
          <Clock className="h-4 w-4 shrink-0" aria-hidden="true" /> Snooze
        </Button>
        <Button
          type="button"
          className="h-12 gap-1.5 px-2"
          disabled={busy}
          aria-label={`Accept ${label}`}
          onClick={() => onAction("accepted")}
        >
          <Check className="h-4 w-4 shrink-0" aria-hidden="true" /> Accept
        </Button>
      </div>
    </article>
  );
}
