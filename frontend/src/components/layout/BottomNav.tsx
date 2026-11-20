import { Link, useLocation } from "react-router-dom";
import { Menu } from "lucide-react";
import { useDecideCount } from "../../hooks/useDecideItems";
import { cn } from "../../lib/utils";
import { isEntryActive, mobileSlots } from "../../lib/routeManifest";

const SLOTS = mobileSlots();

// On the label, not the button: the global `button { font: inherit }` rule would override a size set on a <button>.
const LABEL_CLASS = "text-[11px] font-medium leading-tight";

const ITEM_CLASS =
  "relative flex min-h-[44px] w-full flex-col items-center justify-center gap-0.5 px-1 py-1.5 " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent";

interface BottomNavProps {
  /** Opens the "More" sheet (the full sidebar). */
  onMore: () => void;
  moreOpen?: boolean;
}

/**
 * Phone navigation below `lg`: four destinations from the route manifest plus
 * "More". It sits above the home indicator (safe-area padding); `main` reserves
 * the same height so nothing hides behind it.
 */
export function BottomNav({ onMore, moreOpen = false }: BottomNavProps) {
  const { pathname } = useLocation();
  const pending = useDecideCount();

  return (
    <nav
      aria-label="Quick navigation"
      className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-surface pb-[env(safe-area-inset-bottom)] pl-[env(safe-area-inset-left)] pr-[env(safe-area-inset-right)] lg:hidden"
    >
      <ul className="grid grid-cols-5">
        {SLOTS.map((entry) => {
          const active = isEntryActive(entry, pathname);
          const showBadge = entry.id === "decide" && pending > 0;
          return (
            <li key={entry.id}>
              <Link
                to={entry.to}
                aria-current={active ? "page" : undefined}
                className={cn(ITEM_CLASS, active ? "text-accent" : "text-text-secondary hover:text-text-primary")}
              >
                <span className="relative">
                  <entry.icon className="h-5 w-5" aria-hidden="true" />
                  {showBadge && (
                    <span
                      aria-hidden="true"
                      className="absolute -right-2.5 -top-1.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-danger px-1 text-[10px] font-semibold leading-none text-white"
                    >
                      {pending > 99 ? "99+" : pending}
                    </span>
                  )}
                </span>
                <span className={LABEL_CLASS}>{entry.mobile.label}</span>
                {showBadge && <span className="sr-only">, {pending} waiting</span>}
              </Link>
            </li>
          );
        })}
        <li>
          <button
            type="button"
            onClick={onMore}
            aria-haspopup="dialog"
            aria-expanded={moreOpen}
            className={cn(ITEM_CLASS, moreOpen ? "text-accent" : "text-text-secondary hover:text-text-primary")}
          >
            <Menu className="h-5 w-5" aria-hidden="true" />
            <span className={LABEL_CLASS}>More</span>
          </button>
        </li>
      </ul>
    </nav>
  );
}
