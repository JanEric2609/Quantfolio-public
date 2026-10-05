import { formatDateTime } from "../../../lib/format";
import type { NewsItem } from "../../../lib/api";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "../../../components/ui/sheet";
import { Badge } from "../../../components/ui/badge";
import { ExternalLink } from "lucide-react";

interface NewsDetailSheetProps {
  item: NewsItem | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function NewsDetailSheet({ item, open, onOpenChange }: NewsDetailSheetProps) {
  if (!item) return null;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-[480px] max-w-full overflow-y-auto">
        <SheetHeader>
          <SheetTitle className="text-base">{item.title}</SheetTitle>
          <SheetDescription>
            <div className="flex flex-wrap items-center gap-2 mt-2">
              {item.ticker && <Badge variant="outline" className="font-mono">{item.ticker}</Badge>}
              {item.is_macro && <Badge variant="warning">macro</Badge>}
              {item.sentiment_label && (
                <Badge variant={item.sentiment_label === "positive" ? "success" : item.sentiment_label === "negative" ? "danger" : "secondary"}>
                  {item.sentiment_label}
                </Badge>
              )}
              {item.relevance_score != null && item.relevance_label && (
                <Badge variant={item.relevance_label === "high" ? "success" : item.relevance_label === "medium" ? "warning" : "secondary"}>
                  Relevance: {item.relevance_label}
                </Badge>
              )}
              <span className="text-xs text-text-muted">{item.source}</span>
              <span className="text-xs text-text-muted">{formatDateTime(item.published_at)}</span>
            </div>
          </SheetDescription>
        </SheetHeader>
        <div className="mt-6 space-y-4">
          {item.summary && <p className="text-sm text-text-secondary">{item.summary}</p>}
          {item.sentiment_score != null && (
            <div className="text-xs text-text-muted">
              Sentiment score: {item.sentiment_score}
            </div>
          )}
          {item.relevance_score != null && (
            <div className="text-xs text-text-muted">
              Relevance score: {item.relevance_score}
            </div>
          )}
          {item.relevance_reason && (
            <div className="text-xs text-text-muted">
              {item.relevance_reason}
            </div>
          )}
          {item.url && (
            <a
              href={item.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-sm text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 transition-colors"
            >
              <ExternalLink className="w-3.5 h-3.5" />
              Read full article
            </a>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
