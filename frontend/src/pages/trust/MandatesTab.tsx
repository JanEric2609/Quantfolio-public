import { Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type MandateListItem } from "../../lib/api";
import type { MandatesOutletContext } from "../mandates/mandateContext";
import { TypeNumbers } from "./TypeNumbers";

/**
 * Mandate decisions: the verdict numbers, then the Mandates Accuracy tab,
 * which compares both mandates from the same outlet context the hub provides.
 */
export function MandatesTab() {
  const portfolios = useQuery({
    queryKey: ["llm-portfolio-list"],
    queryFn: () => api<MandateListItem[]>("/api/llm-portfolio/"),
  });

  const context: MandatesOutletContext = {
    portfolios: portfolios.data ?? [],
    portfoliosLoading: portfolios.isLoading,
  };

  return (
    <div className="space-y-4">
      <TypeNumbers type="mandates" />
      <h2 className="text-sm font-semibold text-text-primary">Accuracy by mandate</h2>
      <Outlet context={context} />
    </div>
  );
}
