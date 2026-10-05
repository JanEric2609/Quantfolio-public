import { Info, TriangleAlert } from "lucide-react";
import { Badge } from "../ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "../ui/tooltip";

export type ConcernSeverity = "warn" | "info";

export interface ConcernMeta {
  label: string;
  severity: ConcernSeverity;
  hint: string;
}

/**
 * Known concern slugs → human metadata. Slugs not present here (including
 * dynamic M2-era strings) render raw with `info` severity, so new engine
 * flags surface without a frontend release.
 */
export const CONCERN_LABELS: Record<string, ConcernMeta> = {
  alphacrafter_miner_unavailable: {
    label: "Predictive model unavailable",
    severity: "warn",
    hint: "IC/ICIR shown are placeholders — the factor-validation engine was disabled for this run.",
  },
  alphacrafter_miner_insufficient_panel: {
    label: "Insufficient comparison panel",
    severity: "warn",
    hint: "Too few comparable candidates for the factor engine to rank this symbol reliably.",
  },
  alphacrafter_miner_no_valid_factors: {
    label: "No valid factors evaluated",
    severity: "warn",
    hint: "The miner produced no factors that passed validation for this candidate.",
  },
  alphacrafter_screener_unavailable: {
    label: "Regime screener unavailable",
    severity: "warn",
    hint: "Regime-conditioned factor alignment was skipped for this run.",
  },
  fundamentals_unavailable: {
    label: "Fundamentals unavailable",
    severity: "info",
    hint: "Fundamental data could not be retrieved; fundamental signals were skipped.",
  },
  sentiment_unavailable: {
    label: "Sentiment unavailable",
    severity: "info",
    hint: "News/sentiment data could not be retrieved; sentiment signals were skipped.",
  },
  short_history: {
    label: "Short price history",
    severity: "warn",
    hint: "Price history is shorter than the evaluation window; metrics may be unreliable.",
  },
  data_gap: {
    label: "Gaps in price history",
    severity: "warn",
    hint: "Missing bars in the price history; computed metrics may be distorted.",
  },
  ml_signal_unavailable: {
    label: "No validated ML model",
    severity: "info",
    hint: "One pooled model ranks the Euro Stoxx 50 + S&P 100 on 20-day volatility-scaled returns. It is refitted weekly and used only when its out-of-sample rank IC clears the gate; until then this signal is left out of the score, not counted as neutral.",
  },
  estimate_data_unavailable: {
    label: "No analyst-estimate data",
    severity: "info",
    hint: "Analyst estimate-revision data (IBES) currently only covers US-listed tickers; this signal is skipped, not penalized, for others.",
  },
  analyst_unavailable: {
    label: "Analyst consensus unavailable",
    severity: "warn",
    hint: "No analyst price target or rating could be fetched this run. The analyst signal was left out of the score rather than counted as neutral.",
  },
  insider_data_unavailable: {
    label: "No insider-trading data",
    severity: "info",
    hint: "Insider-trading filings (SEC Form 3/4/5) only exist for US-listed tickers; this signal is skipped, not penalized, in the composite score.",
  },
};

const STALE_PATTERN = /^(\S+) data stale since (.+)$/;
const EARNINGS_PATTERN = /^earnings on (\d{4}-\d{2}-\d{2}) \(in (\d+) days\)$/;

/** Stable DOM/testid slug: lowercase, non-alphanumeric runs → underscores. */
export function concernSlug(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

export function resolveConcernMeta(raw: string): ConcernMeta {
  const known = CONCERN_LABELS[raw];
  if (known) return known;
  const stale = STALE_PATTERN.exec(raw);
  if (stale) {
    return {
      label: `${stale[1]} price data stale`,
      severity: "warn",
      hint: "Data ends at the shown date; metrics may be distorted.",
    };
  }
  const earnings = EARNINGS_PATTERN.exec(raw);
  if (earnings) {
    const days = Number(earnings[2]);
    return {
      label: days === 0 ? "Earnings today" : `Earnings in ${days} day${days === 1 ? "" : "s"}`,
      severity: "warn",
      hint: `The company reports on ${earnings[1]}. The price often moves several times a normal day's range on the report, in either direction. Buying before it is a bet on the result.`,
    };
  }
  return { label: raw, severity: "info", hint: raw };
}

interface ConcernBadgesProps {
  concerns?: string[] | null;
  max?: number;
}

function ConcernBadge({ raw }: { raw: string }) {
  const meta = resolveConcernMeta(raw);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          variant={meta.severity === "warn" ? "warning" : "secondary"}
          data-testid={`concern-badge-${concernSlug(raw)}`}
          className="cursor-help gap-1 px-2 py-0 text-[10px] font-medium"
        >
          {meta.severity === "warn" ? (
            <TriangleAlert className="h-3 w-3" aria-hidden />
          ) : (
            <Info className="h-3 w-3" aria-hidden />
          )}
          {meta.label}
        </Badge>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs">
        <p className="text-xs font-semibold">{meta.label}</p>
        <p className="text-xs leading-relaxed text-text-secondary">{meta.hint}</p>
      </TooltipContent>
    </Tooltip>
  );
}

function MoreConcernsBadge({ hidden }: { hidden: string[] }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          variant="outline"
          data-testid="concern-badge-more"
          className="cursor-help px-2 py-0 text-[10px] font-medium"
        >
          +{hidden.length} more
        </Badge>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs">
        <ul className="space-y-1.5">
          {hidden.map((raw, i) => {
            const meta = resolveConcernMeta(raw);
            return (
              <li key={`${raw}-${i}`} className="space-y-0.5">
                <p className="text-xs font-semibold">
                  {meta.label}{" "}
                  <span className="font-normal text-text-muted">({raw})</span>
                </p>
                <p className="text-xs leading-relaxed text-text-secondary">
                  {meta.hint}
                </p>
              </li>
            );
          })}
        </ul>
      </TooltipContent>
    </Tooltip>
  );
}

export function ConcernBadges({ concerns, max = 4 }: ConcernBadgesProps) {
  const list = (concerns ?? []).filter(
    (c): c is string => typeof c === "string" && c.trim().length > 0,
  );
  if (list.length === 0) return null;

  const visible = list.slice(0, max);
  const hidden = list.slice(max);

  return (
    <TooltipProvider delayDuration={150}>
      <div className="flex flex-wrap items-center gap-1.5">
        {visible.map((raw, i) => (
          <ConcernBadge key={`${raw}-${i}`} raw={raw} />
        ))}
        {hidden.length > 0 && <MoreConcernsBadge hidden={hidden} />}
      </div>
    </TooltipProvider>
  );
}
