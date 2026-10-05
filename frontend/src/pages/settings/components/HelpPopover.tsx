import { HelpCircle, ExternalLink } from "lucide-react";
import type React from "react";
import { Button } from "../../../components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "../../../components/ui/popover";

export function HelpPopover({
  title,
  children,
  docUrl,
}: {
  title: string;
  children: React.ReactNode;
  docUrl?: string | null;
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button type="button" variant="ghost" size="icon" className="h-7 w-7" aria-label={`${title} help`}>
          <HelpCircle className="h-4 w-4" aria-hidden="true" />
        </Button>
      </PopoverTrigger>
      <PopoverContent aria-label={`${title} help`} className="space-y-3">
        <div className="text-sm font-medium">{title}</div>
        <div className="text-sm leading-relaxed text-text-secondary">{children}</div>
        {docUrl && /^https?:\/\//i.test(docUrl) ? (
          <a
            href={docUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-sm text-accent hover:underline"
          >
            Open docs <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
          </a>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
