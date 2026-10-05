import type { TrustTypeKey } from "../../lib/api";
import { Card } from "../../components/ui/card";
import { Skeleton } from "../../components/ui/skeleton";
import { TypeSummary } from "./TypeSummary";
import { useTrustVerdict } from "./useTrust";

/** The trust numbers for one prediction type, shown above the detailed view reused from its own page. */
export function TypeNumbers({ type }: { type: TrustTypeKey }) {
  const query = useTrustVerdict();
  if (query.isLoading) return <Skeleton className="h-32 w-full rounded-md" />;
  const row = query.data?.types.find((t) => t.type === type);
  if (!row) {
    return (
      <Card className="p-4 text-sm text-text-secondary" role="status">
        The track-record numbers are unavailable right now. The detail below still loads on its own.
      </Card>
    );
  }
  return <TypeSummary type={row} />;
}
