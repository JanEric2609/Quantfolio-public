import { Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { ActivityTable } from "./components/ActivityTable";

export function ActivityTab() {
  return (
    <div className="space-y-3">
      {/* Trades lost its tab; the trade log is reached from here. */}
      <div className="flex justify-end">
        <Link to="/portfolio/trades" className="inline-flex items-center gap-1 text-sm font-medium text-accent hover:underline">
          Trade log <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
        </Link>
      </div>
      <ActivityTable />
    </div>
  );
}
