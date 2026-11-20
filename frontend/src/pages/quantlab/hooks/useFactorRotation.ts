import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";

export const useFactorRotation = (windowDays: number = 60) =>
  useQuery({
    queryKey: ["quant", "factors", "rotation", windowDays],
    queryFn: () => api<any>(`/api/quant/factors/rotation?window=${windowDays}`),
  });
