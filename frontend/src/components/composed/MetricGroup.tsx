import { cn } from "../../lib/utils";

export function MetricGroup({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <div>
      {title && <h3 className="text-sm font-medium text-text-secondary mb-2">{title}</h3>}
      <div className={cn("metric-grid gap-4")}>{children}</div>
    </div>
  );
}
