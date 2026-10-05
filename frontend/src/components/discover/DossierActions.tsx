import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Eye, TrendingUp } from "lucide-react";
import { toast } from "sonner";
import { api, type WatchlistItem } from "../../lib/api";
import { Button } from "../ui/button";
import { cn } from "../../lib/utils";

/** Watchlist horizon tag for a discovery horizon in months. */
export function horizonTag(months: number | null | undefined): "short" | "mid" | "long" {
  if (months == null) return "mid";
  if (months <= 3) return "short";
  if (months <= 12) return "mid";
  return "long";
}

/**
 * What to do with a dossier besides reading it: put the symbol on the watchlist
 * (existing `/api/portfolio/watchlist`) or open its stock page. There is no
 * dismiss endpoint for dossiers/candidates in the API, so none is offered here.
 */
export function DossierActions({
  symbol,
  name,
  horizonMonths,
  className,
}: {
  symbol: string;
  name?: string | null;
  horizonMonths?: number | null;
  className?: string;
}) {
  const queryClient = useQueryClient();
  // Same key as the Research → Watchlist page, so both share one cached list.
  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: () => api<WatchlistItem[]>("/api/portfolio/watchlist") });
  const ticker = symbol.toUpperCase();
  const watched = (Array.isArray(watchlist.data) ? watchlist.data : []).some((item) => item.ticker?.toUpperCase() === ticker);

  const watch = useMutation({
    mutationFn: () =>
      api<WatchlistItem>("/api/portfolio/watchlist", {
        method: "POST",
        body: JSON.stringify({
          ticker,
          name: name && name !== symbol ? name : ticker,
          horizon_tag: horizonTag(horizonMonths),
        }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      toast.success(`${ticker} added to your watchlist`);
    },
    onError: (error: Error) => toast.error(error.message),
  });

  return (
    <div
      className={cn("flex flex-wrap items-center gap-2", className)}
      // Shortlist cards are clickable as a whole; these controls must not also open the dossier.
      onClick={(event) => event.stopPropagation()}
    >
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={watched || watch.isPending || watchlist.isLoading}
        aria-label={watched ? `Watching ${ticker}` : `Watch ${ticker}`}
        onClick={() => watch.mutate()}
      >
        {watched ? <Check className="mr-1.5 h-4 w-4" aria-hidden /> : <Eye className="mr-1.5 h-4 w-4" aria-hidden />}
        {watched ? "Watching" : "Watch"}
      </Button>
      <Button asChild variant="outline" size="sm">
        <Link to={`/research/stocks/${encodeURIComponent(ticker)}`} aria-label={`Open stock ${ticker}`}>
          <TrendingUp className="mr-1.5 h-4 w-4" aria-hidden />
          Open stock
        </Link>
      </Button>
    </div>
  );
}
