import { lookupGlossary } from "../../lib/glossary";
import { TapTooltip } from "./TapTooltip";

/**
 * Explains a glossary term. The trigger is a focusable button (tap, click,
 * Enter/Space or keyboard focus opens it). Pass `ariaLabel` (and `iconOnly`, which
 * enlarges the tap area) when `children` is only an icon; text children name the
 * button themselves.
 */
export function GlossaryTooltip({ k, children, ariaLabel, iconOnly = false }: { k: string; children: React.ReactNode; ariaLabel?: string; iconOnly?: boolean }) {
  const entry = lookupGlossary(k);
  if (!entry) return <>{children}</>;
  return (
    <TapTooltip
      ariaLabel={ariaLabel}
      hitArea={iconOnly}
      content={
        <div>
          <p className="font-semibold text-text-primary">{entry.label}</p>
          <p className="text-sm text-text-secondary mt-1">{entry.body}</p>
        </div>
      }
    >
      {children}
    </TapTooltip>
  );
}
