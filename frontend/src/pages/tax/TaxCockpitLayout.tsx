import { useNavigate, useSearchParams, Outlet, useLocation } from "react-router-dom";
import { AlertTriangle } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "../../components/composed/PageHeader";
import { TabNav } from "../../components/composed/TabNav";
import { Badge } from "../../components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
import { api } from "../../lib/api";

const CURRENT_YEAR = new Date().getFullYear();
const TAX_YEARS = [CURRENT_YEAR - 2, CURRENT_YEAR - 1, CURRENT_YEAR, CURRENT_YEAR + 1];

type TaxSettings = { tax_residency_country?: string | null };

const DE_TABS = [
  { to: "/tax/overview", label: "Overview" },
  { to: "/tax/lots", label: "Lots & Events" },
  { to: "/tax/settings", label: "Settings" },
];

const NL_TABS = [
  { to: "/tax/box3", label: "Box 3 (Wealth Tax)" },
  { to: "/tax/settings", label: "Settings" },
];

export default function TaxCockpitLayout() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const year = parseInt(searchParams.get("year") ?? String(CURRENT_YEAR), 10);

  const settingsQuery = useQuery({
    queryKey: ["tax", "settings"],
    queryFn: () => api<{ settings: TaxSettings }>("/api/tax/settings").then((r) => r.settings),
    staleTime: 60_000,
  });

  const jurisdiction = (
    settingsQuery.data?.tax_residency_country ?? "DE"
  ).toUpperCase();

  const isNL = jurisdiction === "NL";
  // The year travels with every tab link.
  const tabs = (isNL ? NL_TABS : DE_TABS).map((tab) => ({ ...tab, to: `${tab.to}${location.search}` }));

  const setYear = (y: string) => {
    const params = new URLSearchParams(searchParams);
    params.set("year", y);
    navigate(`${location.pathname}?${params.toString()}`, { replace: true });
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Tax Cockpit"
        actions={
          <div className="flex items-center gap-3">
            <Badge variant="secondary" className="text-xs">
              {isNL ? "Netherlands — Box 3" : "Germany — KESt / Soli"}
            </Badge>
            <Select value={String(year)} onValueChange={setYear}>
              <SelectTrigger className="w-28">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TAX_YEARS.map((y) => (
                  <SelectItem key={y} value={String(y)}>{y}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        }
      />

      <div className="flex items-center gap-2 rounded-md border border-warn/40 bg-warn/10 p-3 text-sm text-warn">
        <AlertTriangle size={14} />
        {isNL ? (
          <span>Estimate only. Official Belastingdienst assessment remains the source of truth. Not tax advice.</span>
        ) : (
          <span>Estimate only. Broker statements (Steuerbescheinigung, Erträgnisaufstellung) remain the source of truth. This cockpit is not tax advice.</span>
        )}
      </div>

      <TabNav tabs={tabs} ariaLabel="Tax sections" />

      <Outlet context={{ year }} />
    </div>
  );
}
