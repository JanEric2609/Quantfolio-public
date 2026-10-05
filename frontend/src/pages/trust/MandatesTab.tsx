import { Outlet, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Button } from "../../components/ui/button";
import { api, type MandateListItem } from "../../lib/api";
import type { MandateLetter, MandatesOutletContext } from "../mandates/MandatesHub";
import { TypeNumbers } from "./TypeNumbers";

const MANDATES: MandateLetter[] = ["A", "B"];

/**
 * Mandate decisions: the verdict numbers, then the existing Mandates Accuracy
 * tab, which reads its mandate and portfolio from the same outlet context the
 * Mandates hub provides.
 */
export function MandatesTab() {
  const [searchParams, setSearchParams] = useSearchParams();
  const mandate: MandateLetter = searchParams.get("mandate") === "B" ? "B" : "A";

  const portfolios = useQuery({
    queryKey: ["llm-portfolio-list"],
    queryFn: () => api<MandateListItem[]>("/api/llm-portfolio/"),
  });
  const selected = portfolios.data?.find((p) => p.mandate === mandate) ?? null;

  const setMandate = (m: MandateLetter) => {
    const next = new URLSearchParams(searchParams);
    next.set("mandate", m);
    setSearchParams(next, { replace: true });
  };

  const context: MandatesOutletContext = {
    portfolios: portfolios.data ?? [],
    portfoliosLoading: portfolios.isLoading,
    mandate,
    portfolioId: selected?.id ?? null,
  };

  return (
    <div className="space-y-4">
      <TypeNumbers type="mandates" />
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-text-primary">Accuracy by mandate</h2>
        <div role="group" aria-label="Mandate" className="flex items-center gap-1 rounded-md border border-border p-0.5">
          {MANDATES.map((m) => (
            <Button
              key={m}
              type="button"
              size="sm"
              variant={mandate === m ? "default" : "ghost"}
              aria-pressed={mandate === m}
              onClick={() => setMandate(m)}
            >
              Mandate {m}
            </Button>
          ))}
        </div>
      </div>
      <Outlet context={context} />
    </div>
  );
}
