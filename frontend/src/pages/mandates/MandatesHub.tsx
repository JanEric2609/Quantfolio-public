import { Outlet, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Scale } from "lucide-react";
import { PageHeader } from "../../components/composed/PageHeader";
import { TabNav } from "../../components/composed/TabNav";
import { Button } from "../../components/ui/button";
import { api, type MandateListItem } from "../../lib/api";

const TABS = [
  { to: "/mandates/overview", label: "Overview" },
  { to: "/mandates/journal", label: "Journal" },
  { to: "/mandates/accuracy", label: "Accuracy" },
  { to: "/mandates/divergence", label: "Divergence" },
];

const MANDATES = ["A", "B"] as const;
export type MandateLetter = (typeof MANDATES)[number];

/** Shared via <Outlet context>; consumed with useOutletContext in the four tabs. */
export interface MandatesOutletContext {
  portfolios: MandateListItem[];
  portfoliosLoading: boolean;
  mandate: MandateLetter;
  /** The PaperPortfolio id for the selected mandate, or null if that mandate
   * has no portfolio yet (never created via "create-mandate" / reseed). */
  portfolioId: string | null;
}

export function MandatesHub() {
  const [searchParams, setSearchParams] = useSearchParams();
  const mandate: MandateLetter = searchParams.get("mandate") === "B" ? "B" : "A";

  const portfoliosQuery = useQuery({
    queryKey: ["llm-portfolio-list"],
    queryFn: () => api<MandateListItem[]>("/api/llm-portfolio/"),
  });

  const selected = portfoliosQuery.data?.find((p) => p.mandate === mandate) ?? null;

  const setMandate = (m: MandateLetter) => {
    const next = new URLSearchParams(searchParams);
    next.set("mandate", m);
    setSearchParams(next, { replace: true });
  };

  const context: MandatesOutletContext = {
    portfolios: portfoliosQuery.data ?? [],
    portfoliosLoading: portfoliosQuery.isLoading,
    mandate,
    portfolioId: selected?.id ?? null,
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Mandates"
        icon={<Scale className="h-8 w-8 text-accent" />}
        subtitle="The mandate A/B decision loop: weekly LLM reviews, decision journal, outcome accuracy, and paper-vs-real divergence."
        actions={
          <div className="flex items-center gap-1 rounded-md border border-border p-0.5">
            {MANDATES.map((m) => (
              <Button
                key={m}
                type="button"
                size="sm"
                variant={mandate === m ? "default" : "ghost"}
                onClick={() => setMandate(m)}
              >
                Mandate {m}
              </Button>
            ))}
          </div>
        }
      />
      {/* The selected mandate (?mandate=) travels with every tab link. */}
      <TabNav
        tabs={TABS.map((t) => ({ ...t, to: `${t.to}?${searchParams.toString()}` }))}
        ariaLabel="Mandate sections"
      />
      <Outlet context={context} />
    </div>
  );
}
