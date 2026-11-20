import { Outlet, useNavigate } from "react-router-dom";
import { useCallback } from "react";
import { QuantLabHeader } from "./components/QuantLabHeader";

export function QuantLabLayout() {
  const navigate = useNavigate();

  const handleRefresh = useCallback(() => {
    navigate(0);
  }, [navigate]);

  return (
    <div className="space-y-0">
      <QuantLabHeader onRefresh={handleRefresh} />
      {/* A div, not <main>: AppShell already provides the page's main landmark. */}
      <div className="min-w-0 overflow-y-auto p-4">
        <Outlet />
      </div>
    </div>
  );
}
