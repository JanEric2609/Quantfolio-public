import { Link, Outlet, useLocation } from "react-router-dom";
import { FlaskConical } from "lucide-react";
import { TabNav } from "../../components/composed/TabNav";
import { STRATEGY_LAB_TABS, activeStrategyLabTab } from "./strategyLabTabs";

const TABS = STRATEGY_LAB_TABS.map(({ to, label }) => ({ to, label }));

/**
 * Wraps Evidence, the LLM advisor, Mandates, Graduation and the paper portfolio
 * in one "Strategy lab" with a shared tab row and a line on what each means for
 * real money. A pathless layout route: every tab keeps its own URL.
 */
export function StrategyLabLayout() {
  const location = useLocation();
  const active = activeStrategyLabTab(location.pathname);

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-sm text-text-secondary">
          <FlaskConical className="h-4 w-4 text-accent" />
          <span className="font-medium text-text-primary">Strategy lab</span>
          <span>
            Research. What to do with real money is on{" "}
            <Link to="/plan" className="text-accent underline-offset-2 hover:underline">This month</Link>.
          </span>
        </div>
        <TabNav tabs={TABS} ariaLabel="Strategy lab sections" />
        {active && <p className="text-xs text-text-secondary">{active.consequence}</p>}
      </div>
      <Outlet />
    </div>
  );
}
