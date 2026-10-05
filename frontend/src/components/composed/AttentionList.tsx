import { Link } from "react-router-dom";
import { AlertCircle, AlertTriangle, ArrowRight, Info } from "lucide-react";
import type { AttentionItem, AttentionSeverity } from "../../lib/api";
import { cn } from "../../lib/utils";

const SEVERITY: Record<AttentionSeverity, { icon: typeof Info; className: string; label: string }> = {
  error: { icon: AlertCircle, className: "text-danger", label: "Problem" },
  warning: { icon: AlertTriangle, className: "text-warn", label: "Warning" },
  info: { icon: Info, className: "text-info", label: "Suggestion" },
};

/**
 * The "needs attention" rows of `GET /api/settings/attention`: severity icon,
 * what is wrong, and one link to fix it. Shared by the Control Center overview
 * and Home so both read the same list the same way.
 */
export function AttentionList({ items }: { items: AttentionItem[] }) {
  return (
    <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface">
      {items.map((item) => {
        const meta = SEVERITY[item.severity];
        const Icon = meta.icon;
        return (
          <li key={item.id} className="flex items-start gap-3 px-4 py-3">
            <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", meta.className)} aria-label={meta.label} />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-text-primary">{item.title}</p>
              <p className="text-sm text-text-secondary">{item.detail}</p>
            </div>
            <Link to={item.href} className="inline-flex shrink-0 items-center gap-1 text-sm font-medium text-accent hover:underline">
              {item.action} <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
