import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";

export interface LlmFactorProposal {
  name: string;
  formula: string;
  description: string;
  intuition: string;
  expected_regime?: string;
  confidence?: number;
}

export interface LlmProposeResponse {
  count: number;
  proposals: LlmFactorProposal[];
  reasoning?: string;
}

export interface LlmAnalyzeResponse {
  confidence?: number;
  response?: string;
  analysis?: string;
  summary?: string;
  regime_assessment?: string;
  risks?: string[];
  opportunities?: string[];
  factor_implications?: Record<string, string>;
  structured?: {
    recommendation?: string;
    summary?: string;
    key_drivers?: string[];
    risks?: string[];
    [key: string]: unknown;
  };
}

export interface LlmRegimeEvalResponse {
  from_regime: string;
  to_regime: string;
  assessment?: string;
  confidence?: number;
}

export const useLlmPropose = (n = 3) =>
  useQuery({
    queryKey: ["llm-propose", n],
    queryFn: () => api<LlmProposeResponse>(`/api/quant/research/llm-propose?n=${n}`),
    enabled: false,
  });

export const useLlmAnalyze = (focus = "general") =>
  useQuery({
    queryKey: ["llm-analyze", focus],
    queryFn: () => api<LlmAnalyzeResponse>(`/api/quant/research/llm-analyze?focus=${encodeURIComponent(focus.trim())}`),
    enabled: false,
  });

export const useLlmRegimeEval = (from: string, to: string) =>
  useQuery({
    queryKey: ["llm-regime-eval", from, to],
    queryFn: () =>
      api<LlmRegimeEvalResponse>(
        `/api/quant/research/llm-regime-eval?from_regime=${encodeURIComponent(from.trim())}&to_regime=${encodeURIComponent(to.trim())}`
      ),
    enabled: !!from && !!to,
  });
