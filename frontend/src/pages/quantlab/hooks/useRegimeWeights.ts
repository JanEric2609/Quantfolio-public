import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";

/** The jump model's current state and the factor weights it implies. */
export interface RegimeWeights {
  regime_label: string;
  crisis: boolean;
  factor_weights: Record<string, number>;
  /** Always null: the jump model labels a state, it does not give a probability. */
  score: number | null;
  model?: string | null;
  available?: boolean;
  state_since?: string | null;
  as_of?: string | null;
  vix?: number | null;
  reason?: string | null;
}

export const useRegimeWeights = () =>
  useQuery({
    queryKey: ["quant", "regime", "factor-weights"],
    queryFn: () => api<RegimeWeights>("/api/quant/regime/factor-weights"),
    refetchInterval: 3600_000,
  });
