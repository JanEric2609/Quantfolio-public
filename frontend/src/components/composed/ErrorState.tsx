import { AlertTriangle } from "lucide-react";
import { Card, CardContent } from "../ui/card";
import { Button } from "../ui/button";

export function ErrorState({ title = "Error", body, onRetry }: {
  title?: string;
  body?: string;
  onRetry?: () => void;
}) {
  return (
    <Card className="border-danger">
      <CardContent className="flex flex-col items-center justify-center py-8 text-center">
        <AlertTriangle className="h-10 w-10 text-danger" />
        <h3 className="mt-3 text-lg font-display font-semibold text-danger">{title}</h3>
        {body && <p className="mt-1 text-sm text-text-secondary">{body}</p>}
        {onRetry && (
          <Button variant="outline" className="mt-4" onClick={onRetry}>
            Retry
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
