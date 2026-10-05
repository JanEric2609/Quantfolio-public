import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, GripVertical } from "lucide-react";
import { getProviderChain, type ProviderChainEntry } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { cn } from "../../../lib/utils";

function parseOrder(value: string): string[] {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

/**
 * Reorders the provider chain. The stored value is a JSON array string; the
 * live registry supplies each provider's enabled/available state.
 */
export function ProviderChainControl({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const chain = useQuery({ queryKey: ["provider-chain"], queryFn: getProviderChain });
  const [dragging, setDragging] = useState<number | null>(null);

  const status = useMemo(
    () => new Map((chain.data?.providers ?? []).map((provider) => [provider.provider, provider])),
    [chain.data],
  );
  const order = useMemo(() => {
    const stored = parseOrder(value);
    const fromRegistry = (chain.data?.providers ?? []).map((provider) => provider.provider);
    return [...stored, ...fromRegistry.filter((name) => !stored.includes(name))];
  }, [value, chain.data]);

  const move = (from: number, to: number) => {
    if (to < 0 || to >= order.length || from === to) return;
    const next = [...order];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    onChange(JSON.stringify(next));
  };

  if (!order.length) {
    return <p className="text-sm text-text-muted">{chain.isLoading ? "Loading providers…" : "No providers registered."}</p>;
  }

  return (
    <ol className="w-full space-y-1.5 sm:w-96" aria-label="Provider order">
      {order.map((name, index) => {
        const provider: ProviderChainEntry | undefined = status.get(name);
        return (
          <li
            key={name}
            draggable
            onDragStart={() => setDragging(index)}
            onDragOver={(event) => {
              event.preventDefault();
              if (dragging !== null && dragging !== index) {
                move(dragging, index);
                setDragging(index);
              }
            }}
            onDragEnd={() => setDragging(null)}
            className={cn(
              "flex items-center gap-2 rounded-md border border-border bg-surface-2 px-2 py-1.5 text-sm",
              dragging === index && "border-accent",
              provider && !provider.enabled && "opacity-60",
            )}
          >
            <GripVertical className="h-4 w-4 shrink-0 cursor-grab text-text-muted" aria-hidden="true" />
            <span className="w-5 text-right text-xs tabular-nums text-text-muted">{index + 1}</span>
            <span className="flex-1 font-medium">{name}</span>
            <span className={cn("text-xs", provider?.enabled ? "text-success" : "text-text-muted")}>
              {provider ? (provider.enabled ? "active" : "no key") : "unknown"}
            </span>
            <Button type="button" variant="ghost" size="icon" className="h-6 w-6" aria-label={`Move ${name} up`}
              disabled={index === 0} onClick={() => move(index, index - 1)}>
              <ArrowUp className="h-3.5 w-3.5" />
            </Button>
            <Button type="button" variant="ghost" size="icon" className="h-6 w-6" aria-label={`Move ${name} down`}
              disabled={index === order.length - 1} onClick={() => move(index, index + 1)}>
              <ArrowDown className="h-3.5 w-3.5" />
            </Button>
          </li>
        );
      })}
    </ol>
  );
}
