import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuCheckboxItem } from "../../../components/ui/dropdown-menu";
import { Button } from "../../../components/ui/button";
import { Paperclip } from "lucide-react";

interface ContextAttachMenuProps {
  toggles: Record<string, boolean>;
  onChange: (toggles: Record<string, boolean>) => void;
}

export const CONTEXT_OPTIONS = [
  { id: "portfolio", label: "Current portfolio" },
  { id: "dossier", label: "Current dossier" },
  { id: "page", label: "Active page" },
];

export function ContextAttachMenu({ toggles, onChange }: ContextAttachMenuProps) {
  const activeCount = Object.values(toggles).filter(Boolean).length;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="icon" aria-label="Attach context" className="relative">
          <Paperclip className="h-4 w-4" />
          {activeCount > 0 && (
            <span className="absolute -top-1 -right-1 flex h-3 w-3 items-center justify-center rounded-full bg-accent text-[8px] text-accent-fg">
              {activeCount}
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-48">
        {CONTEXT_OPTIONS.map((opt) => (
          <DropdownMenuCheckboxItem
            key={opt.id}
            checked={!!toggles[opt.id]}
            onCheckedChange={(checked) => onChange({ ...toggles, [opt.id]: checked })}
          >
            {opt.label}
          </DropdownMenuCheckboxItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
