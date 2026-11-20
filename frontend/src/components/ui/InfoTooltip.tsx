import { HelpCircle } from "lucide-react";
import { TapTooltip } from "../composed/TapTooltip";

interface Props {
  text: string;
  side?: "top" | "right" | "bottom" | "left";
}

export function InfoTooltip({ text, side = "top" }: Props) {
  return (
    <TapTooltip
      content={text}
      side={side}
      ariaLabel="More info"
      hitArea
      className="ml-1 inline-flex items-center text-text-muted hover:text-text-secondary"
      contentClassName="text-sm"
    >
      <HelpCircle className="h-3.5 w-3.5" aria-hidden />
    </TapTooltip>
  );
}
