import type { LucideIcon } from "lucide-react";
import { Card, CardContent } from "../ui/card";
import { Button } from "../ui/button";

export function EmptyState({
  icon: Icon,
  title,
  description,
  actionLabel,
  onAction,
  busy,
}: {
  icon: LucideIcon;
  title: string;
  description?: string;
  actionLabel?: string;
  onAction?: () => void;
  busy?: boolean;
}) {
  return (
    <Card className="flex flex-col items-center justify-center py-12 text-center">
      <CardContent>
        <Icon className="mx-auto h-12 w-12 text-text-muted" />
        <h3 className="mt-4 text-lg font-display font-semibold">{title}</h3>
        {description && (
          <p className="mt-2 text-sm text-text-secondary">{description}</p>
        )}
        {actionLabel && (
          <Button className="mt-4" disabled={busy} onClick={busy ? undefined : onAction}>
            {actionLabel}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
