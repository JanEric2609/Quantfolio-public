import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Trash2 } from "lucide-react";
import { api, type Holding, type MarketHistoryPoint, type PortfolioTransaction } from "../../../lib/api";
import { formatCurrency, formatDate, formatNumber } from "../../../lib/format";
import { LineChart } from "../../../components/charts/LineChart";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Skeleton } from "../../../components/ui/skeleton";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "../../../components/ui/sheet";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "../../../components/ui/alert-dialog";
import { EditHoldingDialog } from "./EditHoldingDialog";

export function HoldingDetailSheet({ holding, onOpenChange }: { holding: Holding | null; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const history = useQuery({
    queryKey: ["market-history", holding?.ticker, 90],
    queryFn: () => api<MarketHistoryPoint[]>(`/api/market/history/${holding?.ticker}?days=90`),
    enabled: Boolean(holding?.ticker),
  });
  const transactions = useQuery({
    queryKey: ["portfolio-transactions"],
    queryFn: () => api<PortfolioTransaction[]>("/api/portfolio/transactions"),
    enabled: Boolean(holding),
  });
  const remove = useMutation({
    mutationFn: () => api(`/api/portfolio/holdings/${holding?.id}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["holdings"] });
      queryClient.invalidateQueries({ queryKey: ["wealth"] });
      queryClient.invalidateQueries({ queryKey: ["portfolio-activity"] });
      queryClient.invalidateQueries({ queryKey: ["portfolio-snapshots"] });
      toast.success("Holding deleted");
      onOpenChange(false);
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const holdingTransactions = (transactions.data ?? []).filter((row) => row.holding_id === holding?.id).slice(0, 6);
  return (
    <>
      <Sheet open={Boolean(holding)} onOpenChange={onOpenChange}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
          {holding && <>
            <SheetHeader>
              <SheetTitle className="truncate pr-6">{holding.name}</SheetTitle>
              <SheetDescription className="flex flex-wrap gap-2"><Badge variant="secondary">{holding.asset_type}</Badge><span className="font-mono">{holding.ticker}</span><span className="font-mono">{holding.isin}</span></SheetDescription>
            </SheetHeader>
            <div className="mt-6 space-y-5">
              <div className="grid grid-cols-2 gap-3 text-sm">
                <Info label="Quantity" value={formatNumber(Number(holding.quantity), { digits: 4, minDigits: 0 })} />
                <Info label="Average buy" value={holding.avg_buy_price != null ? formatCurrency(Number(holding.avg_buy_price), { currency: holding.currency }) : <span className="text-text-muted">Unknown</span>} />
              </div>
              {history.isLoading ? (
                <Skeleton className="h-56 w-full" />
              ) : history.isError ? (
                <div className="flex items-center justify-between gap-3 rounded-md border border-danger/30 bg-surface-2 p-4">
                  <div className="text-sm text-danger">Failed to load price history.</div>
                  <button onClick={() => history.refetch()} className="rounded-md bg-danger/10 px-3 py-1.5 text-xs font-medium text-danger transition-colors hover:bg-danger/20">Retry</button>
                </div>
              ) : (
                <LineChart className="h-56" ariaLabel={`${holding.ticker} price over 90 days`} series={[{ name: holding.ticker ?? "Price", data: (history.data ?? []).map((row) => [row.date, row.close]) }]} yFormat={(value) => formatCurrency(value, { currency: holding.currency })} />
              )}
              <section>
                <h3 className="mb-2 text-sm font-semibold">Recent transactions</h3>
                <div className="space-y-2">
                  {holdingTransactions.map((row) => <div key={row.id} className="flex justify-between rounded-md border border-border bg-surface-2 p-2 text-sm"><span>{formatDate(row.date)} {row.type}</span><span className="font-mono">{row.quantity} @ {row.price}</span></div>)}
                  {!holdingTransactions.length && <div className="text-sm text-text-muted">No transactions logged for this holding.</div>}
                </div>
              </section>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => setEditing(true)}>Edit</Button>
                <AlertDialog>
                  <AlertDialogTrigger asChild><Button variant="destructive"><Trash2 className="h-4 w-4" />Delete</Button></AlertDialogTrigger>
                  <AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Delete holding?</AlertDialogTitle><AlertDialogDescription>This removes the manual holding and its transaction log.</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction onClick={() => remove.mutate()}>Delete</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
                </AlertDialog>
              </div>
            </div>
          </>}
        </SheetContent>
      </Sheet>
      {holding && <EditHoldingDialog holding={holding} open={editing} onOpenChange={setEditing} />}
    </>
  );
}

function Info({ label, value }: { label: string; value: string | React.ReactNode }) {
  return <div className="rounded-md border border-border bg-surface-2 p-3"><div className="text-xs text-text-secondary">{label}</div><div className="mt-1 font-mono">{value}</div></div>;
}
