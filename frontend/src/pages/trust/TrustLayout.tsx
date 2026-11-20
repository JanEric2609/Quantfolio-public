import { Outlet } from "react-router-dom";
import { Gavel, Lightbulb, ListChecks, Scale, BrainCircuit, FlaskConical } from "lucide-react";
import { PageHeader } from "../../components/composed/PageHeader";
import { TabNav } from "../../components/composed/TabNav";

const tabs = [
  { to: "/trust/verdict", label: "Verdict", icon: Gavel },
  { to: "/trust/ideas", label: "Ideas", icon: Lightbulb },
  { to: "/trust/advisor", label: "Advisor", icon: BrainCircuit },
  { to: "/trust/mandates", label: "Mandates", icon: Scale },
  { to: "/trust/evidence", label: "Evidence", icon: FlaskConical },
  { to: "/trust/calls", label: "Calls", icon: ListChecks },
];

/**
 * "Can I trust it?": is the system's track record real or luck? Tabs are
 * routes, so every one is deep-linkable. Replaces the old Verification page.
 */
export function TrustLayout() {
  return (
    <div className="space-y-5">
      <PageHeader
        title="Can I trust it?"
        subtitle="The system's own predictions, checked against what happened. Every number shows how many calls it rests on."
      />
      <TabNav tabs={tabs} ariaLabel="Trust sections" />
      <Outlet />
    </div>
  );
}
