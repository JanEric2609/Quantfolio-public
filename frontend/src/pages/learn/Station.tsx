import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Card } from "../../components/ui/card";

/** One station: a question, one control, one sentence of feedback, and where the live figure comes from. */
export function Station({
  id, title, question, source, sourceTo, children, empty,
}: {
  id: string;
  title: string;
  question: string;
  source: string;
  sourceTo: string;
  children?: ReactNode;
  /** Set when the live data is missing: the station says why and is skipped. */
  empty?: string | null;
}) {
  return (
    <Card className="space-y-3 p-5" data-testid={`station-${id}`}>
      <h2 className="text-base font-semibold text-text-primary">{title}</h2>
      <p className="text-sm text-text-secondary">{question}</p>
      {empty ? (
        <p className="text-sm text-text-muted" data-testid={`station-${id}-empty`}>{empty} Skipped for now.</p>
      ) : (
        children
      )}
      <p className="text-xs text-text-muted">
        Live figure from <Link to={sourceTo} className="text-accent underline-offset-2 hover:underline">{source}</Link>.
      </p>
    </Card>
  );
}

export function Feedback({ children, show }: { children: ReactNode; show: boolean }) {
  if (!show) return null;
  return <p className="rounded-md bg-surface-2 p-3 text-sm text-text-primary" role="status">{children}</p>;
}
