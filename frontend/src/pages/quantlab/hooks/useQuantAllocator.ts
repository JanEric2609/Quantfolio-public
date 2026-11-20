import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, updateSettings } from "../../../lib/api";
import type { QuantAllocator } from "../../../lib/api";

/** How to split the next contribution across the candidate ETFs; empty ``amount`` uses the monthly-contribution setting. */
export const useQuantAllocator = (opts: { amount?: number; maxWeight?: number }) => {
  const params = new URLSearchParams();
  if (opts.amount != null) params.set("contribution_eur", String(opts.amount));
  if (opts.maxWeight) params.set("max_weight", String(opts.maxWeight));
  const qs = params.toString();
  return useQuery({
    queryKey: ["quant", "portfolio", "allocator", qs],
    queryFn: () => api<QuantAllocator>(`/api/quant/portfolio/allocator${qs ? `?${qs}` : ""}`),
  });
};

/** Saves the candidate list (ISINs) as the ``allocator_universe_isins`` setting and reloads the allocator. */
export const useSaveAllocatorUniverse = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (isins: string[]) => updateSettings({ settings: { allocator_universe_isins: isins.join(", ") } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["quant", "portfolio", "allocator"] }),
  });
};
