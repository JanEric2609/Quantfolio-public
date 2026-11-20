import { cn } from "../../lib/utils";
import type { SignedDelta } from "../../lib/format";

const TONE_CLASS: Record<SignedDelta["tone"], string> = {
  up: "text-success",
  down: "text-danger",
  flat: "text-text-secondary",
};

/**
 * A gain/loss figure that never relies on colour alone: the sign is part of the
 * text, a direction arrow precedes it, and screen readers get the spoken form
 * ("up +5,20 %"). Build the `delta` with `formatSignedDelta`.
 */
export function DeltaText({ delta, className, arrow = true }: { delta: SignedDelta; className?: string; arrow?: boolean }) {
  return (
    <span className={cn("inline-flex items-baseline gap-1 tabular-nums", TONE_CLASS[delta.tone], className)}>
      {arrow && delta.text !== "—" && <span aria-hidden="true" className="text-[0.7em] leading-none">{delta.arrow}</span>}
      <span aria-hidden="true">{delta.text}</span>
      <span className="sr-only">{delta.label}</span>
    </span>
  );
}
