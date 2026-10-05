import type { NewsItem } from "../../../lib/api";
import { NewsCard } from "./NewsCard";

interface NewsGridProps {
  items: NewsItem[];
  onSelect: (item: NewsItem) => void;
}

export function NewsGrid({ items, onSelect }: NewsGridProps) {
  if (!items.length) {
    return (
      <div className="rounded-md border border-border bg-surface-2 p-8 text-center text-sm text-text-muted">
        No news items match the current filters.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-1 md:grid-cols-2 xl:grid-cols-3">
      {items.map((item) => (
        <NewsCard key={item.id} item={item} onClick={() => onSelect(item)} />
      ))}
    </div>
  );
}
