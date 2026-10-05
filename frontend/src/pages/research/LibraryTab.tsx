import { formatDate } from "../../lib/format";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type AnalysisReport } from "../../lib/api";
import { DataTable } from "../../components/composed/DataTable";
import { ReportReader } from "./components/ReportReader";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "../../components/ui/sheet";
import { Button } from "../../components/ui/button";
import type { ColumnDef } from "@tanstack/react-table";

export function LibraryTab() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<AnalysisReport | null>(null);
  const reports = useQuery({ queryKey: ["reports"], queryFn: () => api<AnalysisReport[]>("/api/ai/reports") });

  const analyse = useMutation({
    mutationFn: (id: string) => api<any>(`/api/ai/reports/${id}/analyse`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["recommendations"] }),
  });

  const columns: ColumnDef<AnalysisReport>[] = [
    { accessorKey: "title", header: "Title" },
    { accessorKey: "ticker", header: "Ticker" },
    { accessorKey: "horizon", header: "Horizon" },
    { accessorKey: "source", header: "Source" },
    { accessorFn: (row) => formatDate(row.created_at), header: "Created" },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => (
        <Button size="sm" variant="outline" onClick={(e) => { e.stopPropagation(); analyse.mutate(row.original.id); }}>
          Analyse
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <DataTable
        data={reports.data ?? []}
        columns={columns}
        onRowClick={(row) => setSelected(row)}
        enableFilter
        emptyState={<div className="text-sm text-text-muted py-8 text-center">No reports saved yet.</div>}
      />

      <Sheet open={!!selected} onOpenChange={(open) => { if (!open) setSelected(null); }}>
        <SheetContent side="right" className="w-[640px] max-w-full overflow-y-auto">
          <SheetHeader><SheetTitle>{selected?.title}</SheetTitle></SheetHeader>
          {selected && <ReportReader content={selected.content} />}
        </SheetContent>
      </Sheet>
    </div>
  );
}
