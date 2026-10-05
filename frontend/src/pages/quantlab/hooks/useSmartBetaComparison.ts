import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";

export const useSmartBetaComparison = () =>
  useQuery({
    queryKey: ["quant", "factors", "smart-beta"],
    queryFn: () => api<any>("/api/quant/factors/smart-beta"),
  });
