import { Link, useLocation } from "react-router-dom";
import { cn } from "../../lib/utils";
import { isEntryActive, isResearchToolPath, sidebarGroups, type RouteEntry } from "../../lib/routeManifest";
import { useUiStore } from "../../lib/store";
import { RegimeChip } from "../regime/RegimeChip";
import { Switch } from "../ui/switch";

const GROUPS = sidebarGroups();
const ADMIN_ENTRIES = GROUPS.find((g) => g.id === "admin")?.items ?? [];
const MAIN_GROUPS = GROUPS.filter((g) => g.id !== "admin");

function NavItem({ entry, pathname }: { entry: RouteEntry; pathname: string }) {
  const active = isEntryActive(entry, pathname);
  return (
    <li>
      <Link
        to={entry.to}
        aria-current={active ? "page" : undefined}
        className={cn(
          "flex min-h-11 items-center gap-2 rounded-md border-l-2 border-transparent px-3 py-2 text-sm lg:min-h-0 lg:py-1.5",
          active
            ? "border-accent bg-surface-2 text-accent"
            : "text-text-secondary hover:bg-surface-2 hover:text-text-primary",
        )}
      >
        <entry.icon size={16} aria-hidden="true" />
        <span className="min-w-0 truncate">{entry.label}</span>
      </Link>
    </li>
  );
}

const GROUP_LABEL = "px-2 pb-1 text-[11px] uppercase tracking-wider text-text-secondary";

function PrimaryNav({ pathname }: { pathname: string }) {
  const showResearchTools = useUiStore((s) => s.showResearchTools);
  const setShowResearchTools = useUiStore((s) => s.setShowResearchTools);
  // Standing on a research tool keeps the group open: hiding the item you are
  // on would leave the nav with nothing highlighted.
  const onToolRoute = isResearchToolPath(pathname);
  const toolsOpen = showResearchTools || onToolRoute;

  return (
    <nav aria-label="Primary" className="flex-1 overflow-y-auto py-3">
      {MAIN_GROUPS.map((group) => {
        const isTools = group.id === "tools";
        const labelId = `nav-group-${group.id}`;
        return (
          <div key={group.id} className="px-3 py-2">
            {isTools ? (
              <div className="flex items-center justify-between pb-1 pr-1">
                <span id={labelId} className={cn(GROUP_LABEL, "pb-0")}>{group.label}</span>
                <Switch
                  aria-label="Show research tools"
                  title={onToolRoute ? "Open while you are in a research tool" : undefined}
                  checked={toolsOpen}
                  disabled={onToolRoute}
                  onCheckedChange={setShowResearchTools}
                />
              </div>
            ) : (
              <div id={labelId} className={GROUP_LABEL}>{group.label}</div>
            )}
            {(!isTools || toolsOpen) && (
              <ul aria-labelledby={labelId} className="space-y-0.5">
                {group.items.map((entry) => <NavItem key={entry.id} entry={entry} pathname={pathname} />)}
              </ul>
            )}
          </div>
        );
      })}
    </nav>
  );
}

/**
 * Wordmark, the grouped primary nav, Control Center and the regime chip.
 * The desktop sidebar and the phone's "More" sheet render the same body.
 */
export function SidebarBody() {
  const { pathname } = useLocation();
  return (
    <div className="flex h-full min-h-0 flex-1 flex-col bg-surface">
      <div className="flex h-14 shrink-0 items-center border-b border-border px-5">
        <span className="font-display text-lg font-semibold text-text-primary">Quantfolio</span>
      </div>
      <PrimaryNav pathname={pathname} />
      <div className="shrink-0 space-y-2 border-t border-border px-3 py-3">
        <ul>
          {ADMIN_ENTRIES.map((entry) => <NavItem key={entry.id} entry={entry} pathname={pathname} />)}
        </ul>
        <div className="px-2 pb-[env(safe-area-inset-bottom)]">
          <RegimeChip />
        </div>
      </div>
    </div>
  );
}

export function Sidebar() {
  return (
    <aside className="hidden lg:flex flex-col w-[260px] border-r border-border bg-surface">
      <SidebarBody />
    </aside>
  );
}
