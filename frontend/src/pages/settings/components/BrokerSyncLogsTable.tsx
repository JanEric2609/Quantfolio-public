import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api, type ScalableSyncLog, type Trading212SyncLog } from "../../../lib/api";
import { formatDateTime } from "../../../lib/format";
import { DataTable } from "../../../components/composed/DataTable";
import { BrokerBadge } from "../../../components/portfolio/BrokerFilter";

type BrokerLog = ScalableSyncLog | Trading212SyncLog;

const STATE_CLASS: Record<BrokerLog["state"], string> = {
  success: "border-success/40 text-success",
  warning: "border-warn/40 text-warn",
  error: "border-danger/40 text-danger",
  running: "border-border text-text-secondary",
};

const columns: ColumnDef<BrokerLog>[] = [
  {
    accessorKey: "started_at",
    header: "Date",
    cell: ({ row }) => (row.original.started_at ? formatDateTime(row.original.started_at) : "—"),
  },
  { accessorKey: "source", header: "Broker", cell: ({ row }) => <BrokerBadge source={row.original.source} /> },
  { accessorKey: "trigger", header: "Started" },
  {
    accessorKey: "state",
    header: "State",
    cell: ({ row }) => (
      <span className={`rounded-md border px-2 py-0.5 text-xs ${STATE_CLASS[row.original.state] ?? STATE_CLASS.running}`}>
        {row.original.state}
      </span>
    ),
  },
  {
    accessorKey: "message",
    header: "Message",
    cell: ({ row }) => <span className="line-clamp-1" title={row.original.message}>{row.original.message}</span>,
  },
];

/** Scalable and Trading 212 syncs, newest first. Both sync without a TAN, so the worker runs them on a schedule. */
export function BrokerSyncLogsTable() {
  const scalable = useQuery({ queryKey: ["scalable-sync-logs"], queryFn: () => api<ScalableSyncLog[]>("/api/scalable/sync/logs") });
  const trading212 = useQuery({ queryKey: ["trading212-sync-logs"], queryFn: () => api<Trading212SyncLog[]>("/api/trading212/sync/logs") });
  const logs = [...(scalable.data ?? []), ...(trading212.data ?? [])].sort((a, b) =>
    (b.started_at ?? "").localeCompare(a.started_at ?? ""),
  );
  return (
    <div>
      {(scalable.isLoading || trading212.isLoading) && <div className="text-sm text-text-muted">Loading sync logs…</div>}
      {(scalable.isError || trading212.isError) && (
        <div className="mb-3 rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
          Failed to load the {scalable.isError ? "Scalable" : "Trading 212"} sync logs
        </div>
      )}
      {!scalable.isLoading && !trading212.isLoading && <DataTable data={logs} columns={columns} pageSize={25} />}
    </div>
  );
}
