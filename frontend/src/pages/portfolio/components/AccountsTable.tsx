import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Card, CardContent } from "../../../components/ui/card";
import { Badge } from "../../../components/ui/badge";
import { Skeleton } from "../../../components/ui/skeleton";
import { DataTable } from "../../../components/composed/DataTable";
import { BrokerBadge, BrokerFilterToggle, brokerOf, matchesBroker, useBrokerFilter } from "../../../components/portfolio/BrokerFilter";
import { api, type WealthSummary } from "../../../lib/api";
import { formatCurrency, formatDateTime } from "../../../lib/format";

type Account = WealthSummary["accounts"][number];

type Position = WealthSummary["positions"][number];

/**
 * A depot's own cash balance is 0 and says nothing; what it holds is its
 * securities. Matched by depot id, else by broker when the broker has one depot.
 */
export function depotSecuritiesValue(account: Account, accounts: Account[], positions: Position[]): number | null {
  if (account.type !== "depot") return null;
  const own = positions.filter((p) => p.account_id === account.id);
  if (own.length > 0) return own.reduce((sum, p) => sum + p.current_value, 0);
  const depotsOfBroker = accounts.filter((a) => a.type === "depot" && a.source === account.source);
  if (depotsOfBroker.length !== 1) return null;
  const byBroker = positions.filter((p) => p.source === account.source);
  return byBroker.length > 0 ? byBroker.reduce((sum, p) => sum + p.current_value, 0) : null;
}

function buildColumns(accounts: Account[], positions: Position[]): ColumnDef<Account>[] {
  return [
  { accessorKey: "name", header: "Name" },
  {
    accessorKey: "source",
    header: "Source",
    cell: ({ row }) =>
      brokerOf(row.original.source) ? <BrokerBadge source={row.original.source} /> : <Badge variant="secondary">{row.original.source}</Badge>,
  },
  { accessorKey: "institution", header: "Institution", cell: ({ row }) => row.original.institution ?? "-" },
  { accessorKey: "type", header: "Type" },
  {
    accessorKey: "balance",
    header: "Balance",
    cell: ({ row }) => {
      const account = row.original;
      if (account.type === "depot") {
        const value = depotSecuritiesValue(account, accounts, positions);
        if (value == null) return <span className="text-text-secondary">-</span>;
        return (
          <span className="font-mono">
            {formatCurrency(value, { currency: account.currency })}{" "}
            <span className="font-sans text-xs text-text-secondary">securities</span>
          </span>
        );
      }
      return <span className="font-mono">{formatCurrency(account.balance, { currency: account.currency })}</span>;
    },
  },
  { accessorKey: "last_synced", header: "Last synced", cell: ({ row }) => formatDateTime(row.original.last_synced) },
  ];
}

export function AccountsTable({ rightSlot }: { rightSlot?: React.ReactNode }) {
  const accounts = useQuery({ queryKey: ["portfolio-accounts"], queryFn: () => api<Account[]>("/api/portfolio/accounts") });
  const wealth = useQuery({ queryKey: ["wealth"], queryFn: () => api<WealthSummary>("/api/portfolio/wealth") });
  const columns = useMemo(
    () => buildColumns(accounts.data ?? [], wealth.data?.positions ?? []),
    [accounts.data, wealth.data],
  );
  const brokerFilter = useBrokerFilter((accounts.data ?? []).map((account) => account.source));
  const shown = (accounts.data ?? []).filter((account) => matchesBroker(account.source, brokerFilter.filter));

  return (
    <Card>
      <CardContent className="pt-6">
        {accounts.isLoading ? (
          <div className="space-y-3">
            {[...Array(5)].map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}
          </div>
        ) : accounts.isError ? (
          <div className="flex items-center justify-between gap-3">
            <div className="text-sm text-danger">Failed to load accounts.</div>
            <button onClick={() => accounts.refetch()} className="rounded-md bg-danger/10 px-3 py-1.5 text-xs font-medium text-danger transition-colors hover:bg-danger/20">Retry</button>
          </div>
        ) : (
          <div className="space-y-3">
            <BrokerFilterToggle brokers={brokerFilter.brokers} value={brokerFilter.filter} onChange={brokerFilter.setFilter} />
            <DataTable data={shown} columns={columns} enableFilter csvFilename="portfolio-accounts.csv" rightSlot={rightSlot} emptyState="No connected accounts yet." />
          </div>
        )}
      </CardContent>
    </Card>
  );
}
