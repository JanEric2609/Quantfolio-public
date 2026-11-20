import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";
export const useObsidianSyncStatus = () => useQuery({ queryKey: ["obsidian-sync-status"], queryFn: () => api<any>("/api/quant/research/obsidian-sync-status") });
