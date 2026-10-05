import { useRef, useState, type ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "../ui/tooltip";
import { cn } from "../../lib/utils";

/**
 * A tooltip whose trigger is a real `<button type="button">`.
 *
 * Radix tooltips open on hover and keyboard focus only, so on a phone (no hover;
 * a tap does not open them) and for anyone who cannot hover, the explanation was
 * unreachable. Here the trigger also toggles on tap and on Enter/Space, closes
 * on Escape or an outside tap, and a mouse still gets hover plus click-to-pin.
 */
export function TapTooltip({
  content,
  children,
  ariaLabel,
  side = "top",
  className,
  contentClassName,
  delayDuration = 150,
  hitArea = false,
}: {
  content: ReactNode;
  children: ReactNode;
  /** Required when `children` is an icon with no text. */
  ariaLabel?: string;
  side?: "top" | "bottom" | "left" | "right";
  className?: string;
  contentClassName?: string;
  delayDuration?: number;
  /** For small icon triggers: extends the tappable area ~12 px on every side without moving the layout. */
  hitArea?: boolean;
}) {
  const [open, setOpen] = useState(false);
  // What the pointer/keyboard did before `click` fires: Radix dismisses an open
  // tooltip on the outside pointer-down that the trigger itself causes, so the
  // click handler needs the state from *before* that happened to toggle right.
  const gesture = useRef<{ wasOpen: boolean; pointer: string | null }>({ wasOpen: false, pointer: null });

  return (
    <TooltipProvider delayDuration={delayDuration}>
      <Tooltip open={open} onOpenChange={setOpen}>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-label={ariaLabel}
            className={cn(
              "inline cursor-help appearance-none border-0 bg-transparent p-0 text-left align-baseline font-[inherit] text-inherit",
              "rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-bg",
              hitArea && "relative after:absolute after:-inset-3 after:content-['']",
              className,
            )}
            onPointerDown={(event) => {
              gesture.current = { wasOpen: open, pointer: event.pointerType };
            }}
            onClick={(event) => {
              const { wasOpen, pointer } = gesture.current;
              gesture.current = { wasOpen: false, pointer: null };
              // Keep Radix from closing the tooltip on click; we decide below.
              event.preventDefault();
              if (pointer === "mouse") {
                setOpen(true);
              } else if (pointer === null) {
                setOpen((current) => !current); // keyboard-initiated click
              } else {
                setOpen(!wasOpen); // touch / pen
              }
            }}
          >
            {children}
          </button>
        </TooltipTrigger>
        <TooltipContent side={side} className={cn("max-w-xs", contentClassName)}>
          {content}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
