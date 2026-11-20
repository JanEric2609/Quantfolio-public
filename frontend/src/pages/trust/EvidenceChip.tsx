import { CircleDashed, ShieldCheck, ShieldAlert, ShieldX } from "lucide-react";
import type { TrustTypeVerdict } from "../../lib/api";
import { Badge } from "../../components/ui/badge";
import { STATE_META, chipLabel } from "./trustFormat";

const ICON = {
  too_early: CircleDashed,
  skill: ShieldCheck,
  no_evidence: ShieldAlert,
  harm: ShieldX,
} as const;

/** The evidence state as a chip: colour plus icon plus words, never colour alone. */
export function EvidenceChip({
  type,
}: {
  type: Pick<TrustTypeVerdict, "state" | "n" | "n_needed" | "benchmarked" | "type"> & { n_needed_is_lower_bound?: boolean };
}) {
  const Icon = ICON[type.state];
  const meta = STATE_META[type.state];
  return (
    <Badge variant={meta.badge} className="gap-1.5 px-3 py-1 text-xs" data-state={type.state} title={meta.meaning}>
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {chipLabel(type)}
    </Badge>
  );
}
