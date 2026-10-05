import { useState, useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, ExternalLink, MoreHorizontal, Plus } from "lucide-react";
import { toast } from "sonner";
import { api, type WatchlistItem, type RecommendationDetail } from "../../lib/api";
import { useQuotes } from "../../hooks/useQuotes";
import { usePriceMoves } from "../../hooks/usePriceMoves";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Input } from "../../components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import {
  AlertDialog,
  AlertDialogTrigger,
  AlertDialogContent,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogCancel,
  AlertDialogAction,
} from "../../components/ui/alert-dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "../../components/ui/dropdown-menu";
import { formatCurrency, formatDate, formatSignedDelta } from "../../lib/format";
import { DeltaText } from "../../components/composed/DeltaText";
import { AddWatchlistDialog } from "./components/AddWatchlistDialog";

type WatchlistRow = WatchlistItem & { price?: number; stale: boolean; dayChange?: number };

export function WatchlistTab() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [targetInputs, setTargetInputs] = useState<Record<string, string>>({});
  const [deleteId, setDeleteId] = useState<string | null>(null);

  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: () => api<WatchlistItem[]>("/api/portfolio/watchlist") });
  const quotes = useQuotes((watchlist.data ?? []).map((row) => row.ticker));
  const moves = usePriceMoves((watchlist.data ?? []).map((row) => row.ticker));

  const rows = useMemo(() => (watchlist.data ?? []).map((row) => ({
    ...row,
    price: row.ticker ? quotes.data?.[row.ticker]?.price : undefined,
    stale: row.ticker ? quotes.data?.[row.ticker]?.stale ?? true : true,
    dayChange: row.ticker ? moves.data?.[row.ticker]?.deltaPct : undefined,
  })), [moves.data, quotes.data, watchlist.data]);

  const remove = useMutation({
    mutationFn: (id: string) => api(`/api/portfolio/watchlist/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      toast.success("Watchlist item deleted");
    },
    onError: (error: Error) => toast.error(error.message),
    onSettled: () => setDeleteId(null),
  });

  const setAlert = useMutation({
    mutationFn: ({ id, targetPrice }: { id: string; targetPrice: number | null }) =>
      api(`/api/portfolio/watchlist/${id}/alert`, { method: "PATCH", body: JSON.stringify({ target_price: targetPrice }) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      toast.success("Alert updated");
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const analyse = useMutation({
    mutationFn: (ticker: string) => api<RecommendationDetail>("/api/ai/analyse-report", { method: "POST", body: JSON.stringify({ ticker, strategy: "buy_hold" }) }),
    // The endpoint returns the saved payload without an id, and /recommendations
    // is a retired redirect; the stock page is where the analysis is read.
    onSuccess: (_data, ticker) => {
      toast.success(`Analysis saved for ${ticker}`);
      navigate(`/research/stocks/${encodeURIComponent(ticker)}`);
    },
    onError: (error: Error) => toast.error(error.message),
  });

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Watchlist</CardTitle>
          <AddWatchlistDialog />
        </CardHeader>
        <CardContent>
          {!watchlist.data?.length ? (
            <p className="text-sm text-text-muted py-8 text-center">No watchlist items yet. Add symbols above.</p>
          ) : (
            <div className="space-y-3">
              {(rows ?? []).map((item) => (
                <div key={item.id} className="flex flex-col gap-3 rounded-md border border-border bg-surface-2 p-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0 flex-1">
                    <button
                      onClick={() => item.ticker && navigate(`/research/stocks/${item.ticker}`)}
                      type="button"
                      className="text-sm font-semibold hover:text-accent transition-colors cursor-pointer flex items-center gap-1.5"
                    >
                      {item.ticker ?? item.name}
                      {item.ticker && <ExternalLink size={12} className="text-text-muted" />}
                    </button>
                    <div className="text-xs text-text-muted">{item.name} · {item.horizon_tag}</div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                      <span className="inline-flex gap-1 font-mono">
                        {formatCurrency(item.price)}
                        {item.stale && <Badge variant="warning">stale</Badge>}
                      </span>
                      {item.dayChange == null ? (
                        <span className="text-text-muted">—</span>
                      ) : (
                        <DeltaText delta={formatSignedDelta(item.dayChange, "percent")} />
                      )}
                      <span className="text-text-muted">Added {formatDate(item.added_date)}</span>
                    </div>
                    {/* Alert UI */}
                    <div className="mt-2 flex items-center gap-2">
                      {item.target_price != null ? (
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant={item.alert_triggered ? "success" : "secondary"}>
                            Target: {formatCurrency(Number(item.target_price))}
                          </Badge>
                          {item.alert_triggered && <Bell className="h-3.5 w-3.5 text-success" />}
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => setAlert.mutate({ id: item.id, targetPrice: null })}
                            disabled={setAlert.isPending}
                          >
                            Clear alert
                          </Button>
                        </div>
                      ) : (
                        <div className="flex flex-wrap items-center gap-2">
                          <Input
                            type="number"
                            inputMode="decimal"
                            aria-label={`Target price for ${item.ticker ?? item.name}`}
                            step="0.01"
                            placeholder="Target price"
                            value={targetInputs[item.id] ?? ""}
                            onChange={(e) => setTargetInputs((prev) => ({ ...prev, [item.id]: e.target.value }))}
                            className="w-32 sm:h-7 sm:text-xs"
                          />
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={!targetInputs[item.id] || setAlert.isPending}
                            onClick={() => {
                              const parsed = parseFloat(targetInputs[item.id] ?? "");
                              if (!Number.isFinite(parsed) || parsed <= 0) {
                                toast.error("Please enter a valid positive target price");
                                return;
                              }
                              setAlert.mutate({ id: item.id, targetPrice: parsed }, {
                                onSuccess: () => setTargetInputs((prev) => ({ ...prev, [item.id]: "" })),
                              });
                            }}
                          >
                            Set alert
                          </Button>
                        </div>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 self-end sm:self-auto">
                    <Button size="sm" variant="accent" onClick={() => item.ticker && analyse.mutate(item.ticker)} disabled={analyse.isPending || !item.ticker}>
                      Generate dossier
                    </Button>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="icon" aria-label={`Actions for ${item.ticker}`}>
                          <MoreHorizontal className="h-4 w-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onClick={() => item.ticker && navigate(`/research/stocks/${item.ticker}`)}>
                          Research report
                        </DropdownMenuItem>
                        {/* No "move to portfolio": holdings come from the broker sync, never a made-up unit. */}
                        <DropdownMenuItem className="text-danger" onClick={() => setDeleteId(item.id)}>
                          Remove from watchlist
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <AlertDialog open={deleteId !== null} onOpenChange={(v) => !v && setDeleteId(null)}>
        <AlertDialogContent className="bg-surface border-border">
          <AlertDialogHeader>
            <AlertDialogTitle>Delete watchlist item?</AlertDialogTitle>
            <AlertDialogDescription>
              This will permanently remove this item from your watchlist. This action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => { if (deleteId) remove.mutate(deleteId); }}
              className="bg-danger hover:bg-danger/90"
              disabled={remove.isPending}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
