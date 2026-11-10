import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { formatPercentPoints } from "../../lib/format";

// The plan API reports shares in percent units (12.5 = 12,5 %), not fractions.
export function pct(value: number): string {
  return formatPercentPoints(value, { digits: value >= 10 || value === 0 ? 0 : 1 });
}

/** A collapsed section: the information stays, the page stays short. */
export function Details({ title, summary, children }: { title: string; summary?: string; children: ReactNode }) {
  return (
    <details className="group rounded-lg border border-border bg-surface-1">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-medium text-text-primary">
        <ChevronRight className="h-4 w-4 shrink-0 transition-transform group-open:rotate-90" />
        {title}
        {summary && <span className="ml-auto min-w-0 truncate text-xs font-normal text-text-secondary">{summary}</span>}
      </summary>
      <div className="space-y-3 border-t border-border p-4">{children}</div>
    </details>
  );
}

