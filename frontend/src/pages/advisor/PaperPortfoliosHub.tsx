import { Outlet } from "react-router-dom";
import { PageHeader } from "../../components/composed/PageHeader";
import { TabNav } from "../../components/composed/TabNav";
import { BrainCircuit } from "lucide-react";

const TABS = [
  { to: "/advisor/loop", label: "Advisor Loop" },
  { to: "/advisor/evolution", label: "Evolution" },
  { to: "/advisor/divergence", label: "What would change" },
  { to: "/advisor/diagnostics", label: "Diagnostics" },
];

export function PaperPortfoliosHub() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Advisor"
        icon={<BrainCircuit className="h-8 w-8 text-accent" />}
        subtitle="The advisor loop: daily paper trades → champion/challenger evolution → graduation check. Paper only: it never sets weights for your real book; only This month does."
      />
      <TabNav tabs={TABS} ariaLabel="Advisor sections" />
      <Outlet />
    </div>
  );
}
