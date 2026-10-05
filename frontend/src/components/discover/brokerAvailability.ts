/** Per-broker tradeability from a candidate's ``tradeable_json`` (decision/discover/tradeability.py). */
export interface BrokerAvailability {
  key: "dkb" | "scalable";
  label: string;
  likely: boolean | null;
  confidence: string;
  note: string | null;
  url: string | null;
}

const LABELS = { dkb: "DKB", scalable: "Scalable" } as const;

function str(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

/**
 * One row per broker. Runs before 2026-10 stored only the DKB verdict at the
 * top level; those show DKB alone rather than guessing Scalable.
 */
export function brokerAvailability(tradeable: Record<string, unknown> | null | undefined): BrokerAvailability[] {
  if (!tradeable || Object.keys(tradeable).length === 0) return [];
  const brokers = tradeable.brokers;
  if (brokers && typeof brokers === "object") {
    return (["dkb", "scalable"] as const)
      .filter((key) => key in (brokers as Record<string, unknown>))
      .map((key) => {
        const b = (brokers as Record<string, Record<string, unknown>>)[key];
        return {
          key,
          label: LABELS[key],
          likely: typeof b.likely === "boolean" ? b.likely : null,
          confidence: str(b.confidence) ?? "unknown",
          note: str(b.note),
          url: str(b.url),
        };
      });
  }
  return [{
    key: "dkb",
    label: LABELS.dkb,
    likely: typeof tradeable.likely_tradeable === "boolean" ? tradeable.likely_tradeable : null,
    confidence: str(tradeable.confidence) ?? "unknown",
    note: null,
    url: str(tradeable.manual_check_url),
  }];
}
