import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { Command } from "cmdk";
import { Search } from "lucide-react";
import { Dialog, DialogContent, DialogTitle } from "../ui/dialog";
import { useCommandStore } from "../../lib/hotkeys";
import { useUiStore } from "../../lib/store";
import { paletteEntries } from "../../lib/routeManifest";

// Built from the route manifest, the same list as the sidebar and the bottom
// nav, so the palette cannot drift from them. The manifest also lists pages
// that have no sidebar item of their own (tabs, details).
const PAGES = paletteEntries();

interface CommandItem {
  id: string;
  label: string;
  group: string;
  run?: () => void;
  icon?: React.ComponentType<{ className?: string }>;
  keywords?: string[];
}

export function CommandPalette() {
  const open = useUiStore((s) => s.searchOpen);
  const setSearchOpen = useUiStore((s) => s.setSearchOpen);
  const density = useUiStore((s) => s.density);
  const setDensity = useUiStore((s) => s.setDensity);
  const navigate = useNavigate();
  const commands = useCommandStore((s) => s.commands);

  const items: CommandItem[] = useMemo(
    () => [
      ...PAGES.map((p) => ({ id: p.to, label: p.label, group: "Pages", icon: p.icon, keywords: p.keywords })),
      {
        id: "toggle-density",
        label: "Toggle density",
        group: "Actions",
        run: () => setDensity(density === "compact" ? "comfortable" : "compact"),
      },
      {
        id: "refresh-current-view",
        label: "Refresh current view",
        group: "Actions",
        run: () => window.location.reload(),
      },
      ...commands.map((c) => ({ id: c.id, label: c.label, group: c.group, run: c.run })),
    ],
    [commands, density, setDensity],
  );

  const handleSelect = (item: CommandItem) => {
    const page = PAGES.find((p) => p.to === item.id);
    if (page) navigate(page.to);
    else item.run?.();
    setSearchOpen(false);
  };

  return (
    <Dialog open={open} onOpenChange={setSearchOpen}>
      <DialogContent aria-describedby={undefined} className="max-w-lg p-0">
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <Command className="p-3">
          <div className="flex items-center border-b border-border px-2 pb-3">
            <Search className="mr-2 h-4 w-4 text-text-muted" />
            <Command.Input
              placeholder="Search…"
              className="flex-1 bg-transparent text-sm text-text-primary outline-none placeholder:text-text-muted"
            />
          </div>
          <Command.List className="max-h-72 overflow-y-auto py-2">
            <Command.Empty className="px-3 py-4 text-center text-sm text-text-muted">No results.</Command.Empty>
            <Command.Group heading="Pages" className="px-2 py-1 text-xs text-text-muted">
              {items
                .filter((i) => i.group === "Pages")
                .map((item) => (
                  <Command.Item
                    key={item.id}
                    value={item.label}
                    keywords={item.keywords}
                    onSelect={() => handleSelect(item)}
                    className="flex min-h-11 cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-surface-2 data-[selected=true]:bg-surface-2 sm:min-h-0"
                  >
                    {item.icon && <item.icon className="h-4 w-4 text-text-muted" />}
                    {item.label}
                  </Command.Item>
                ))}
            </Command.Group>
            {items.some((i) => i.group !== "Pages") && (
              <Command.Group heading="Actions" className="px-2 py-1 text-xs text-text-muted">
                {items
                  .filter((i) => i.group !== "Pages")
                  .map((item) => (
                    <Command.Item
                      key={item.id}
                      value={item.label}
                      onSelect={() => handleSelect(item)}
                      className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-surface-2"
                    >
                      {item.label}
                    </Command.Item>
                  ))}
              </Command.Group>
            )}
          </Command.List>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
