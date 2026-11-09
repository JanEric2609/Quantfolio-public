import { Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Scale } from "lucide-react";
import { PageHeader } from "../../components/composed/PageHeader";
import { TabNav } from "../../components/composed/TabNav";
import { api, type MandateListItem } from "../../lib/api";
import type { MandatesOutletContext } from "./mandateContext";

const TABS = [
  { to: "/mandates/overview", label: "Overview" },
  { to: "/mandates/journal", label: "Journal" },
  { to: "/mandates/accuracy", label: "Accuracy" },
  { to: "/mandates/divergence", label: "Divergence" },
];

export function MandatesHub() {
  const portfoliosQuery = useQuery({
    queryKey: ["llm-portfolio-list"],
    queryFn: () => api<MandateListItem[]>("/api/llm-portfolio/"),
  });

  const context: MandatesOutletContext = {
    portfolios: portfoliosQuery.data ?? [],
    portfoliosLoading: portfoliosQuery.isLoading,
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Mandates"
        icon={<Scale className="h-8 w-8 text-accent" />}
        subtitle="Two LLM paper portfolios, reviewed every week and compared side by side: decision journal, outcome accuracy, and paper-vs-real divergence."
      />
      <TabNav tabs={TABS} ariaLabel="Mandate sections" />
      <Outlet context={context} />
    </div>
  );
}
