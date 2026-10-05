import { Link, NavLink, useLocation, useSearchParams } from "react-router-dom";
import type { LucideIcon } from "lucide-react";
import { cn } from "../../lib/utils";

/** One tab. `to` is a route path; set `param` instead for a `?tab=` tab set. */
export interface TabNavItem {
  label: string;
  to?: string;
  /** Value of the `?tab=` search parameter (search-param tab sets). */
  param?: string;
  icon?: LucideIcon;
  end?: boolean;
  /** Custom active logic, e.g. for a tab that owns several child routes. */
  match?: (pathname: string) => boolean;
}

interface TabNavProps {
  tabs: TabNavItem[];
  /** Names the tab set for screen readers, e.g. "Portfolio sections". */
  ariaLabel: string;
  /** Search parameter used by `param` tabs. Defaults to "tab". */
  paramName?: string;
  /** Active `param` when the URL has none. Defaults to the first tab. */
  defaultParam?: string;
  className?: string;
}

const tabClass = (active: boolean) =>
  cn(
    "flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-sm transition-colors",
    "min-h-11 sm:min-h-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 rounded-t-sm",
    active ? "border-accent text-accent" : "border-transparent text-text-secondary hover:text-text-primary",
  );

/**
 * The one tab row used by every sectioned page. Tabs are links (router
 * children or `?tab=`), so every tab is deep-linkable and survives a reload.
 * On a phone the row scrolls sideways instead of pushing the page wider.
 */
export function TabNav({ tabs, ariaLabel, paramName = "tab", defaultParam, className }: TabNavProps) {
  const location = useLocation();
  const [params] = useSearchParams();
  const current = params.get(paramName) ?? defaultParam ?? tabs.find((t) => t.param)?.param;

  return (
    <nav aria-label={ariaLabel} className={cn("-mx-4 border-b border-border px-4 lg:mx-0 lg:px-0", className)}>
      <div className="flex gap-1 overflow-x-auto overscroll-x-contain [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          const content = (
            <>
              {Icon ? <Icon className="h-4 w-4" aria-hidden="true" /> : null}
              {tab.label}
            </>
          );
          if (tab.param !== undefined) {
            const next = new URLSearchParams(params);
            next.set(paramName, tab.param);
            const active = current === tab.param;
            return (
              <Link
                key={tab.param}
                to={{ pathname: location.pathname, search: `?${next.toString()}` }}
                replace
                aria-current={active ? "page" : undefined}
                className={tabClass(active)}
              >
                {content}
              </Link>
            );
          }
          const to = tab.to ?? "";
          const forced = tab.match ? tab.match(location.pathname) : undefined;
          return (
            <NavLink
              key={to}
              to={to}
              end={tab.end}
              aria-current={forced === true ? "page" : undefined}
              className={({ isActive }) => tabClass(forced ?? isActive)}
            >
              {content}
            </NavLink>
          );
        })}
      </div>
    </nav>
  );
}
