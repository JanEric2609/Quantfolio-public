import { formatDate } from "../../../lib/format";
import type { NewsItem } from "../../../lib/api";
import { cn } from "../../../lib/utils";

interface NewsCardProps {
  item: NewsItem;
  onClick: () => void;
}

const SENTIMENT_COLORS: Record<string, string> = {
  positive: "bg-success/10 text-success border-success/30",
  neutral: "bg-surface-3 text-text-secondary border-border",
  negative: "bg-danger/10 text-danger border-danger/30",
};

const RELEVANCE_COLORS: Record<string, string> = {
  high: "bg-success/10 text-success border-success/30",
  medium: "bg-warn/10 text-warn border-warn/30",
  low: "bg-surface-3 text-text-muted border-border",
};

const RELEVANCE_LABEL: Record<string, string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
};

export function NewsCard({ item, onClick }: NewsCardProps) {
  return (
    <article
      className="group cursor-pointer rounded-md border border-border bg-surface-2 p-4 hover:border-accent/50 transition-colors"
      onClick={onClick}
    >
      <div className="flex items-start justify-between gap-2 mb-2">
        <h3 className="text-sm font-semibold line-clamp-2 group-hover:text-accent transition-colors">{item.title}</h3>
        {item.sentiment_label && (
          <span className={cn(
            "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium border",
            SENTIMENT_COLORS[item.sentiment_label] ?? SENTIMENT_COLORS.neutral,
          )}>
            {item.sentiment_label}
          </span>
        )}
        {item.relevance_score != null && item.relevance_label && (
          <span className={cn(
            "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium border",
            RELEVANCE_COLORS[item.relevance_label] ?? RELEVANCE_COLORS.low,
          )}>
            {RELEVANCE_LABEL[item.relevance_label] ?? item.relevance_label}
          </span>
        )}
      </div>
      {item.summary && (
        <p className="text-xs text-text-secondary line-clamp-3 mb-3">{item.summary}</p>
      )}
      <div className="flex flex-wrap items-center gap-2 text-[10px] text-text-muted">
        {item.ticker ? (
          <span className="rounded bg-surface-3 px-1.5 py-0.5 font-mono">{item.ticker}</span>
        ) : item.is_macro ? (
          <span className="rounded bg-warn/10 text-warn px-1.5 py-0.5">macro</span>
        ) : null}
        <span>{item.source}</span>
        <span>{formatDate(item.published_at)}</span>
      </div>
    </article>
  );
}
