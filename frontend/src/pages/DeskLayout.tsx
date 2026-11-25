import { Outlet } from "react-router-dom";
import { Gauge, Activity, Radio } from "lucide-react";
import { PageHeader } from "../components/composed/PageHeader";
import { TabNav } from "../components/composed/TabNav";

// One desk, three views: the paper knock-outs and warrants, the index CFD board, and the event-desk signals.
const DESK_TABS = [
  { to: "/desk", label: "Knock-outs & warrants", icon: Gauge, end: true },
  { to: "/desk/intraday", label: "Intraday CFD", icon: Activity },
  { to: "/desk/signals", label: "Signals", icon: Radio },
];

export function DeskLayout() {
  return (
    <div className="space-y-6">
      <PageHeader title="Derivatives desk" subtitle="The knock-out and warrant desk, the index CFD board and the event-desk signals: all paper, all manual." />
      <TabNav tabs={DESK_TABS} ariaLabel="Desk sections" />
      <Outlet />
    </div>
  );
}
