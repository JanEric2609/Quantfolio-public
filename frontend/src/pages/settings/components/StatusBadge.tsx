import { AlertCircle, CheckCircle2, Loader2, MinusCircle } from "lucide-react";
import { Badge } from "../../../components/ui/badge";
import { cn } from "../../../lib/utils";

export type SettingsStatus = "connected" | "not-configured" | "error" | "testing";

const STATUS_COPY: Record<SettingsStatus, { label: string; className: string; icon: typeof CheckCircle2 }> = {
  connected: { label: "Connected", className: "border-transparent bg-success text-white", icon: CheckCircle2 },
  "not-configured": { label: "Not configured", className: "border-border bg-surface-2 text-text-secondary", icon: MinusCircle },
  error: { label: "Error", className: "border-transparent bg-danger text-white", icon: AlertCircle },
  testing: { label: "Testing", className: "border-transparent bg-info text-white", icon: Loader2 },
};

export function StatusBadge({ status, className }: { status: SettingsStatus; className?: string }) {
  const meta = STATUS_COPY[status];
  const Icon = meta.icon;
  return (
    <Badge variant="outline" className={cn("gap-1.5", meta.className, className)}>
      <Icon className={cn("h-3.5 w-3.5", status === "testing" && "animate-spin")} aria-hidden="true" />
      {meta.label}
    </Badge>
  );
}
