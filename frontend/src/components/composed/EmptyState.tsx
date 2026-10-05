import { LucideIcon } from "lucide-react";
import { Card, CardContent } from "../ui/card";
import { Button } from "../ui/button";

export function EmptyState({ icon: Icon, title, body, cta, onCta, description, actionLabel, onAction }: {
  icon: LucideIcon;
  title: string;
  /** @deprecated Use `description` instead. Kept for backward compat. */
  body?: string;
  /** @deprecated Use `actionLabel` instead. Kept for backward compat. */
  cta?: string;
  /** @deprecated Use `onAction` instead. Kept for backward compat. */
  onCta?: () => void;
  description?: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  const resolvedBody = body ?? description;
  const resolvedCta = cta ?? actionLabel;
  const resolvedOnCta = onCta ?? onAction;

  return (
    <Card className="flex flex-col items-center justify-center py-12 text-center">
      <CardContent>
        <Icon className="mx-auto h-12 w-12 text-text-muted" />
        <h3 className="mt-4 text-lg font-display font-semibold">{title}</h3>
        {resolvedBody && <p className="mt-2 text-sm text-text-secondary">{resolvedBody}</p>}
        {resolvedCta && (
          <Button className="mt-4" onClick={resolvedOnCta}>
            {resolvedCta}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
