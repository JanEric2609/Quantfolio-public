import { useQuery } from "@tanstack/react-query";
import { api, type PortfolioProjection } from "../../../lib/api";

export interface ProjectionParams {
  years: number;
  goal?: number;
  contribution?: number;
  realReturn?: number;
}

/** Monte Carlo projection of the book in real EUR; empty fields use the book and the settings. */
export const useProjection = (p: ProjectionParams) => {
  const q = new URLSearchParams({ years: String(p.years) });
  if (p.goal != null && p.goal > 0) q.set("goal_eur", String(p.goal));
  if (p.contribution != null) q.set("contribution_eur", String(p.contribution));
  if (p.realReturn != null) q.set("real_return", String(p.realReturn));
  return useQuery({
    queryKey: ["quant", "projection", q.toString()],
    queryFn: () => api<PortfolioProjection>(`/api/quant/portfolio/projection?${q.toString()}`),
    staleTime: 10 * 60_000,
  });
};
