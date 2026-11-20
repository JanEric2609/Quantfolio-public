import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";
export const useCorrelationMatrix = () => useQuery({ queryKey: ["quant", "corr"], queryFn: () => api<any>("/api/quant/correlation-matrix") });
export const useRollingCorrelation = (a?: string, b?: string, days = 60) => useQuery({ enabled: Boolean(a && b), queryKey: ["quant", "corr", "rolling", a, b, days], queryFn: () => api<any>(`/api/quant/correlation/rolling?a=${encodeURIComponent(a!)}&b=${encodeURIComponent(b!)}&days=${days}`) });
