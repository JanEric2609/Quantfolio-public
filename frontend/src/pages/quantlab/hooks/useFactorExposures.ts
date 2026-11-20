import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";

export const useFactorExposures = () =>
  useQuery({
    queryKey: ["quant", "factors", "dashboard"],
    queryFn: () => api<any>("/api/quant/factors/dashboard"),
  });
