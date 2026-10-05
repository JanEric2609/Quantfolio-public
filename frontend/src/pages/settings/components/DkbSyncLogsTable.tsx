import { useQuery } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import { DataTable } from "../../../components/composed/DataTable";
import type { ColumnDef } from "@tanstack/react-table";

type SyncLog = {
  id: string;
  session_id: string;
  provider: string;
  state: string;
  message: string;
  created_at: string;
};

export function DkbSyncLogsTable() {
  const logs = useQuery<SyncLog[]>({
    queryKey: ["dkb-sync-logs"],
    queryFn: () => api<SyncLog[]>("/api/dkb/sync/logs"),
  });

  const columns: ColumnDef<SyncLog>[] = [
    {
      accessorKey: "created_at",
      header: "Date",
      cell: ({ getValue }) => new Date(getValue() as string).toLocaleString(),
    },
    {
      accessorKey: "session_id",
      header: "Session ID",
      cell: ({ getValue }) => (
        <span className="font-mono text-xs">{(getValue() as string).slice(0, 8)}…</span>
      ),
    },
    {
      accessorKey: "provider",
      header: "Provider",
    },
    {
      accessorKey: "state",
      header: "State",
      cell: ({ getValue }) => {
        const state = getValue() as string;
        return (
          <span className={`rounded-md border px-2 py-0.5 text-xs ${
            state === "confirmed"
              ? "border-success/40 text-success"
              : state === "failed"
              ? "border-danger/40 text-danger"
              : "border-border text-text-secondary"
          }`}>
            {state}
          </span>
        );
      },
    },
    {
      accessorKey: "message",
      header: "Message",
      cell: ({ getValue }) => <span className="line-clamp-1">{getValue() as string}</span>,
    },
  ];

  return (
    <div>
      {logs.isLoading && <div className="text-sm text-text-muted">Loading sync logs…</div>}
      {logs.isError && (
        <div className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
          Failed to load sync logs
        </div>
      )}
      {!logs.isLoading && !logs.isError && (
        <DataTable data={logs.data ?? []} columns={columns} pageSize={25} />
      )}
    </div>
  );
}
