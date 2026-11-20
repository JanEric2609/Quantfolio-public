import { Link, Outlet } from "react-router-dom";
import { Briefcase, Wallet, Activity, TrendingUp, Target, Gauge, FileBarChart } from "lucide-react";
import { PageHeader } from "../../components/composed/PageHeader";
import { TabNav } from "../../components/composed/TabNav";
import { Button } from "../../components/ui/button";
import { breadcrumbFor } from "../../lib/routeManifest";
import { SummaryStrip } from "./components/SummaryStrip";

// Paper portfolio moved to the Strategy lab and Trades hangs off Activity; both
// URLs keep working, they just no longer take a slot in this row.
const tabs = [
  { to: "/portfolio/holdings", label: "Holdings", icon: Briefcase },
  { to: "/portfolio/performance", label: "Performance", icon: TrendingUp },
  { to: "/portfolio/risk", label: "Risk", icon: Gauge },
  { to: "/portfolio/drift", label: "Targets & drift", icon: Target },
  { to: "/portfolio/accounts", label: "Accounts", icon: Wallet },
  {
    to: "/portfolio/activity",
    label: "Activity",
    icon: Activity,
    match: (pathname: string) => pathname.startsWith("/portfolio/activity") || pathname === "/portfolio/trades",
  },
];

export function PortfolioLayout() {
  return (
    <div className="space-y-5">
      <PageHeader
        title="Portfolio"
        breadcrumb={breadcrumbFor("/portfolio")}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link to="/portfolio/analysis"><FileBarChart className="mr-2 h-4 w-4" aria-hidden="true" /> Analysis report</Link>
          </Button>
        }
      />
      <SummaryStrip />
      <TabNav tabs={tabs} ariaLabel="Portfolio sections" />
      <Outlet />
    </div>
  );
}
