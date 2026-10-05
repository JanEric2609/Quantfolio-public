import { Search } from "lucide-react";
import { useUiStore } from "../../lib/store";
import { cn } from "../../lib/utils";
import { DensityToggle } from "../composed/DensityToggle";
import { NotificationCenter } from "../composed/NotificationCenter";
import { UserMenu } from "../composed/UserMenu";
import { useRouteMeta } from "./useRouteMeta";

/**
 * The page's h1 belongs to its `PageHeader`, never to this bar. The bar shows
 * the route title as plain text: on phones always (it is the only orientation
 * there), on desktop only for pages that have no `PageHeader` of their own.
 */
export function TopBar() {
  const { title, noPageHeader } = useRouteMeta();
  const setSearchOpen = useUiStore((s) => s.setSearchOpen);

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-border bg-surface pl-[max(1rem,env(safe-area-inset-left))] pr-[max(1rem,env(safe-area-inset-right))]">
      <p
        className={cn(
          "min-w-0 truncate font-display text-sm font-semibold text-text-primary lg:text-[1rem]",
          !noPageHeader && "lg:hidden",
        )}
        data-testid="topbar-title"
      >
        {title}
      </p>
      <div className="flex-1" />
      <button
        type="button"
        onClick={() => setSearchOpen(true)}
        className="hidden lg:flex items-center gap-2 h-8 px-3 rounded-md border border-border bg-surface-2 text-sm text-text-secondary hover:text-text-primary"
        aria-label="Search (Cmd+K)"
      >
        <Search size={14} />
        <span>Search…</span>
        <kbd className="ml-2 text-[10px] font-mono px-1.5 py-0.5 rounded border border-border text-text-muted">⌘K</kbd>
      </button>
      <button
        type="button"
        onClick={() => setSearchOpen(true)}
        className="lg:hidden flex h-11 w-11 items-center justify-center rounded-md hover:bg-surface-2"
        aria-label="Search"
      >
        <Search size={18} className="text-text-secondary" />
      </button>
      <div className="hidden lg:block">
        <DensityToggle />
      </div>
      <NotificationCenter />
      <UserMenu />
    </header>
  );
}
