import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "../../lib/utils";

/**
 * A collapsed section: a one-line title and an optional one-line summary stay
 * visible, the detail opens on demand. Native <details>, so it works without
 * JS state and the content stays in the DOM (searchable, testable).
 */
export function Disclosure({
  title, summary, children, defaultOpen = false, className, testId,
}: {
  title: ReactNode;
  summary?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  className?: string;
  testId?: string;
}) {
  return (
    <details
      open={defaultOpen}
      data-testid={testId}
      className={cn("group rounded-lg border border-border bg-surface", className)}
    >
      <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3 [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-4 w-4 shrink-0 text-text-muted transition-transform group-open:rotate-90" aria-hidden="true" />
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold text-text-primary">{title}</span>
          {summary ? <span className="block text-xs text-text-secondary">{summary}</span> : null}
        </span>
      </summary>
      <div className="space-y-3 border-t border-border p-4">{children}</div>
    </details>
  );
}
