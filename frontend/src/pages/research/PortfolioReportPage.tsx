import { useQuery, useMutation } from "@tanstack/react-query";
import {
  ArrowLeft, RefreshCw, Clock, TrendingUp, Wallet,
  AlertTriangle, BarChart3, Target, PieChart,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { api, type PortfolioReportResponse } from "../../lib/api";
import { formatCurrency } from "../../lib/format";
import { PageHeader } from "../../components/composed/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import ReactMarkdown from "react-markdown";
import { toast } from "sonner";

/* ---------- helpers ---------- */

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

/* ---------- main component ---------- */

export function PortfolioReportPage() {
  const navigate = useNavigate();

  const { data: reportData, isLoading, refetch } = useQuery<PortfolioReportResponse>({
    queryKey: ["portfolio-report"],
    queryFn: () => api("/api/research/portfolio-report"),
    staleTime: 60 * 60 * 1000,
  });

  const regenerate = useMutation({
    mutationFn: () => api<PortfolioReportResponse>("/api/research/portfolio-report", { method: "POST" }),
    onSuccess: () => {
      toast.success("Portfolio report regenerated");
      refetch();
    },
    onError: () => toast.error("Failed to regenerate report"),
  });

  const report = reportData?.report;
  const context = reportData?.context;
  const allocation = context?.portfolio_summary?.allocation_pct ?? {};
  const positions = context?.positions ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        level={2}
        title="Portfolio Analysis"
        subtitle="AI-powered portfolio-level analysis synthesizing all holdings"
        actions={
          <div className="flex items-center gap-3">
            {reportData?.generated_at && (
              <span className="text-sm text-muted-foreground flex items-center gap-1">
                <Clock size={14} />
                {timeAgo(reportData.generated_at)}
                {reportData.cached && <Badge variant="outline" className="ml-1 text-xs">cached</Badge>}
              </span>
            )}
            <Button
              variant="outline"
              size="sm"
              onClick={() => regenerate.mutate()}
              disabled={regenerate.isPending}
            >
              <RefreshCw size={14} className={regenerate.isPending ? "animate-spin" : ""} />
              Regenerate
            </Button>
          </div>
        }
      />

      {/* Portfolio Summary Cards */}
      {context && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <Card>
            <CardContent className="p-4">
              <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
                <Wallet size={14} />
                Total Value
              </div>
              <div className="text-2xl font-bold">
                {formatCurrency(context.portfolio_summary.total_value, context.portfolio_summary.currency)}
              </div>
              <div className="text-xs text-muted-foreground mt-1">
                Cash: {formatCurrency(context.portfolio_summary.cash_value, context.portfolio_summary.currency)}
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
                <BarChart3 size={14} />
                Securities
              </div>
              <div className="text-2xl font-bold">
                {formatCurrency(context.portfolio_summary.security_value, context.portfolio_summary.currency)}
              </div>
              <div className="text-xs text-muted-foreground mt-1">
                {positions.length} position{positions.length !== 1 ? "s" : ""}
              </div>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
                <TrendingUp size={14} />
                30-Day Cashflow
              </div>
              <div className={`text-2xl font-bold ${context.portfolio_summary.cashflow_30d.net >= 0 ? "text-success" : "text-danger"}`}>
                {formatCurrency(context.portfolio_summary.cashflow_30d.net, context.portfolio_summary.currency)}
              </div>
              <div className="text-xs text-muted-foreground mt-1">
                In: {formatCurrency(context.portfolio_summary.cashflow_30d.income, context.portfolio_summary.currency)} / Out: {formatCurrency(context.portfolio_summary.cashflow_30d.outflow, context.portfolio_summary.currency)}
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Allocation + Positions */}
      {context && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Allocation Breakdown */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <PieChart size={16} />
                Asset Allocation
              </CardTitle>
            </CardHeader>
            <CardContent>
              {Object.keys(allocation).length > 0 ? (
                <div className="space-y-3">
                  {Object.entries(allocation)
                    .sort(([, a], [, b]) => (b as number) - (a as number))
                    .map(([type, pct]) => (
                      <div key={type} className="flex items-center gap-3">
                        <div className="w-24 text-sm capitalize">{type}</div>
                        <div className="flex-1 h-4 bg-muted rounded-full overflow-hidden">
                          <div
                            className="h-full bg-primary/70 rounded-full transition-all"
                            style={{ width: `${Math.min(pct as number, 100)}%` }}
                          />
                        </div>
                        <div className="w-12 text-sm text-right font-mono">{pct as number}%</div>
                      </div>
                    ))}
                </div>
              ) : (
                <div className="text-sm text-muted-foreground">No allocation data available</div>
              )}
            </CardContent>
          </Card>

          {/* Positions Table */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <Target size={16} />
                Top Positions
              </CardTitle>
            </CardHeader>
            <CardContent>
              {positions.length > 0 ? (
                <div className="space-y-2">
                  {positions
                    .sort((a, b) => b.value - a.value)
                    .slice(0, 10)
                    .map((pos, i) => (
                      <div
                        key={i}
                        className="flex items-center justify-between py-1.5 border-b border-border/50 last:border-0 cursor-pointer hover:bg-muted/50"
                        onClick={() => pos.ticker && navigate(`/research/stocks/${pos.ticker}`)}
                      >
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium">{pos.name}</span>
                          {pos.ticker && (
                            <span className="text-xs text-muted-foreground font-mono">{pos.ticker}</span>
                          )}
                        </div>
                        <div className="flex items-center gap-3">
                          <span className="text-sm text-muted-foreground">{pos.pct}%</span>
                          <span className="text-sm font-mono">{formatCurrency(pos.value, context.portfolio_summary.currency)}</span>
                        </div>
                      </div>
                    ))}
                </div>
              ) : (
                <div className="text-sm text-muted-foreground">No positions found</div>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {/* LLM Report */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <AlertTriangle size={16} />
            AI Portfolio Analysis
          </CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-3">
              {Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className="h-4 bg-muted rounded animate-pulse" style={{ width: `${85 - i * 8}%` }} />
              ))}
            </div>
          ) : report ? (
            <div className="prose prose-invert prose-sm max-w-none">
              <ReactMarkdown>{report}</ReactMarkdown>
            </div>
          ) : (
            <div className="text-sm text-muted-foreground">
              No report available. Click "Regenerate" to generate one.
            </div>
          )}
        </CardContent>
      </Card>

      {/* Error banner */}
      {reportData?.error && (
        <Card className="border-warn/30">
          <CardContent className="p-4">
            <div className="flex items-center gap-2 text-warn text-sm">
              <AlertTriangle size={14} />
              Report generation partially failed: {reportData.error}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Back link */}
      <Button variant="ghost" size="sm" onClick={() => navigate("/research/watchlist")}>
        <ArrowLeft size={14} className="mr-1" />
        Back to Research
      </Button>
    </div>
  );
}
