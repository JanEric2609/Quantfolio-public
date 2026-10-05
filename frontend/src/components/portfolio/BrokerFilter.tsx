import { useMemo } from "react";
import { Badge } from "../ui/badge";
import { ToggleGroup, ToggleGroupItem } from "../ui/toggle-group";
import { useUiStore, type BrokerFilter } from "../../lib/store";
import { cn } from "../../lib/utils";

export type SyncedBroker = Exclude<BrokerFilter, "all">;

const BROKER_SHORT: Record<SyncedBroker, string> = { dkb: "DKB", scalable: "Scalable" };
const BROKER_ORDER: SyncedBroker[] = ["dkb", "scalable"];

/** The synced broker behind a position or account `source`; null for a hand-entered one. */
export function brokerOf(source?: string | null): SyncedBroker | null {
  return source === "dkb" || source === "scalable" ? source : null;
}

export function matchesBroker(source: string | null | undefined, filter: BrokerFilter): boolean {
  return filter === "all" || brokerOf(source) === filter;
}

/**
 * The filter a view applies, and the brokers it can offer. A stored filter for
 * a broker this view has nothing from falls back to "all", so a page never
 * shows an empty list just because another page was left on that broker.
 */
export function useBrokerFilter(sources: Iterable<string | null | undefined>) {
  const stored = useUiStore((state) => state.brokerFilter);
  const setFilter = useUiStore((state) => state.setBrokerFilter);
  const present = new Set<SyncedBroker>();
  for (const source of sources) {
    const broker = brokerOf(source);
    if (broker) present.add(broker);
  }
  const key = BROKER_ORDER.filter((broker) => present.has(broker)).join("|");
  const brokers = useMemo(() => (key ? (key.split("|") as SyncedBroker[]) : []), [key]);
  const filter: BrokerFilter = stored !== "all" && brokers.includes(stored) ? stored : "all";
  return { filter, brokers, setFilter };
}

/** All / DKB / Scalable. Renders nothing until positions from two brokers exist. */
export function BrokerFilterToggle({
  brokers,
  value,
  onChange,
  className,
}: {
  brokers: SyncedBroker[];
  value: BrokerFilter;
  onChange: (value: BrokerFilter) => void;
  className?: string;
}) {
  if (brokers.length < 2) return null;
  return (
    <ToggleGroup
      type="single"
      value={value}
      onValueChange={(next) => next && onChange(next as BrokerFilter)}
      aria-label="Show positions from"
      className={cn("justify-start", className)}
    >
      <ToggleGroupItem value="all" size="sm">All</ToggleGroupItem>
      {brokers.map((broker) => (
        <ToggleGroupItem key={broker} value={broker} size="sm">{BROKER_SHORT[broker]}</ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}

/** Where a position sits: "DKB", "Scalable", or "Manual" for a hand-entered holding. */
export function BrokerBadge({ source, className }: { source?: string | null; className?: string }) {
  const broker = brokerOf(source);
  return (
    <Badge variant={broker ? "outline" : "secondary"} className={cn("shrink-0 px-1.5 py-0 text-[10px] font-medium", className)}>
      {broker ? BROKER_SHORT[broker] : "Manual"}
    </Badge>
  );
}
