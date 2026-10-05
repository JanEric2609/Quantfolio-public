import { useMutation, useQuery } from "@tanstack/react-query";
import { api, type BacktestRun, type BacktestRunRequest, type BacktestStrategy } from "../../../lib/api";

export const useBacktestStrategies = () =>
  useQuery({
    queryKey: ["quant", "backtest", "strategies"],
    queryFn: () => api<BacktestStrategy[]>("/api/quant/backtest/strategies"),
    staleTime: Infinity,
  });

/** Runs one timing rule; every run is recorded as a trial in the global ledger. */
export const useBacktest = () =>
  useMutation({
    mutationFn: (body: BacktestRunRequest) =>
      api<BacktestRun>("/api/quant/backtest/run", { method: "POST", body: JSON.stringify(body) }),
  });
