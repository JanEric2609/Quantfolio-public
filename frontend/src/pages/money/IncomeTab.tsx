import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { IncomeTable, type IncomeSourceRow } from "./components/IncomeTable";
import { AddIncomeSourceDialog, type IncomeSourceFormData } from "./components/AddIncomeSourceDialog";
import { toast } from "sonner";

export function IncomeTab() {
  const queryClient = useQueryClient();
  const incomeSources = useQuery({ queryKey: ["income-sources"], queryFn: () => api<IncomeSourceRow[]>("/api/budget/income-sources") });
  const refresh = () => {
    ["income-sources", "envelopes"].forEach((key) => queryClient.invalidateQueries({ queryKey: [key] }));
  };
  const create = useMutation({ mutationFn: (data: IncomeSourceFormData) => api("/api/budget/income-sources", { method: "POST", body: JSON.stringify(data) }), onSuccess: () => { refresh(); toast.success("Income source added"); }, onError: (e: Error) => toast.error(e.message) });
  const toggle = useMutation({ mutationFn: (row: IncomeSourceRow) => api(`/api/budget/income-sources/${row.id}`, { method: "PUT", body: JSON.stringify({ ...row, active: !row.active }) }), onSuccess: () => { refresh(); toast.success("Updated"); }, onError: (e: Error) => toast.error(e.message) });
  const remove = useMutation({ mutationFn: (id: string) => api(`/api/budget/income-sources/${id}`, { method: "DELETE" }), onSuccess: () => { refresh(); toast.success("Deleted"); }, onError: (e: Error) => toast.error(e.message) });
  return (
    <IncomeTable
      data={incomeSources.data ?? []}
      onToggleActive={(row) => toggle.mutate(row)}
      onDelete={(id) => remove.mutate(id)}
      loading={incomeSources.isLoading}
      rightSlot={<AddIncomeSourceDialog onSave={(data) => create.mutate(data)} pending={create.isPending} />}
    />
  );
}
