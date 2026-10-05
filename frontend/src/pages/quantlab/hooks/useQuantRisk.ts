import { useQuery } from "@tanstack/react-query";
import { api, type QuantPortfolioRisk } from "../../../lib/api";

/**
 * Risk of the book against a benchmark, both in EUR and paired by date.
 * An empty benchmark uses the configured one (MSCI World in EUR); an empty
 * risk-free rate uses the ECB deposit rate the backend resolves. `riskFreePct`
 * is in percent.
 */
export const useQuantRisk = (benchmark = "", confidence = "95", riskFreePct = "") => {
  const params = new URLSearchParams({ confidence: confidence === "99" ? "0.99" : "0.95" });
  if (benchmark.trim()) params.set("benchmark", benchmark.trim().toUpperCase());
  const rf = Number.parseFloat(riskFreePct.replace(",", "."));
  if (riskFreePct.trim() && Number.isFinite(rf)) params.set("risk_free", String(rf / 100));
  return useQuery({
    queryKey: ["quant", "portfolio", "risk", params.toString()],
    queryFn: () => api<QuantPortfolioRisk>(`/api/quant/portfolio/risk?${params.toString()}`),
  });
};
