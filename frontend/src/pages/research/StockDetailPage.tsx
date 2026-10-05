import { useParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation } from "@tanstack/react-query";
import {
  ArrowLeft, TrendingUp, Minus,
  RefreshCw, Clock, Newspaper, BarChart3, Target,
  AlertTriangle, CheckCircle2, XCircle, PieChart,
} from "lucide-react";
import { api, type StockContext, type MultiHorizonVerdict, type ResearchReportResponse, type VerdictsResponse, type PiotroskiResponse, type SentimentResponse, type EtfProfile, type TrackingStats } from "../../lib/api";
import { useState } from "react";
import { PageHeader } from "../../components/composed/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "../../components/ui/tabs";
import { EChart } from "../../components/charts/EChart";
import type { EChartsOption } from "echarts";
import ReactMarkdown from "react-markdown";
import { toast } from "sonner";
import { InfoTooltip } from "../../components/ui/InfoTooltip";
import { formatCompact, formatNumber, formatPercent, formatPercentPoints } from "../../lib/format";

/* ---------- helpers ---------- */

function formatPrice(v: number | undefined | null): string {
  return formatNumber(v, { digits: 2 });
}

function timeAgo(iso: string): string {
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return "—";
  const diff = Date.now() - ts;
  if (diff < 0) return "just now";
  const mins = Math.floor(diff / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

function verdictIcon(v: string) {
  if (v === "BUY") return <CheckCircle2 size={14} className="text-success" />;
  if (v === "SELL") return <XCircle size={14} className="text-danger" />;
  return <Minus size={14} className="text-warn" />;
}

function verdictColor(v: string) {
  if (v === "BUY") return "bg-success/15 text-success border-success/30";
  if (v === "SELL") return "bg-danger/15 text-danger border-danger/30";
  return "bg-warn/15 text-warn border-warn/30";
}

/* ---------- candlestick chart ---------- */

function buildCandlestickOption(
  data: { date: string; open: number; high: number; low: number; close: number; volume: number }[],
): EChartsOption {
  const dates = data.map((d) => d.date);
  const ohlc = data.map((d) => [d.open, d.close, d.low, d.high]);
  const volumes = data.map((d) => d.volume);
  const colors = data.map((d) => (d.close >= d.open ? "#22c55e" : "#ef4444"));

  return {
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
    },
    grid: [
      { left: 60, right: 24, top: 16, height: "58%" },
      { left: 60, right: 24, top: "76%", height: "16%" },
    ],
    xAxis: [
      { type: "category", data: dates, gridIndex: 0, axisLine: { lineStyle: { color: "#262626" } }, axisLabel: { color: "#b3b3b3", show: false }, splitLine: { show: false } },
      { type: "category", data: dates, gridIndex: 1, axisLine: { lineStyle: { color: "#262626" } }, axisLabel: { color: "#b3b3b3" }, splitLine: { show: false } },
    ],
    yAxis: [
      { type: "value", gridIndex: 0, axisLine: { lineStyle: { color: "#262626" } }, axisLabel: { color: "#b3b3b3" }, splitLine: { lineStyle: { color: "#1a1a1a" } } },
      { type: "value", gridIndex: 1, axisLine: { lineStyle: { color: "#262626" } }, axisLabel: { color: "#b3b3b3" }, splitLine: { show: false }, splitNumber: 2 },
    ],
    series: [
      {
        name: "OHLC",
        type: "candlestick",
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: ohlc,
        itemStyle: {
          color: "#22c55e",
          color0: "#ef4444",
          borderColor: "#22c55e",
          borderColor0: "#ef4444",
        },
      },
      {
        name: "Volume",
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumes.map((v, i) => ({
          value: v,
          itemStyle: { color: colors[i], opacity: 0.5 },
        })),
      },
    ],
  };
}

/* ---------- sentiment badge ---------- */

function SentimentBadge({ label, score }: { label?: string; score?: number | null }) {
  if (!label) return null;
  const color =
    label === "positive"
      ? "bg-success/15 text-success border-success/30"
      : label === "negative"
        ? "bg-danger/15 text-danger border-danger/30"
        : "bg-surface-2 text-text-secondary border-border";
  return (
    <Badge variant="outline" className={`text-[10px] ${color}`}>
      {label}{score != null ? ` ${formatNumber(score, { digits: 2 })}` : ""}
    </Badge>
  );
}

function buildBarChartOption(data: { label: string; value: number }[]): import("echarts").EChartsOption {
  const sorted = [...data].sort((a, b) => b.value - a.value);
  return {
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
    grid: { left: 80, right: 24, top: 24, bottom: 24 },
    xAxis: { type: "value", axisLine: { lineStyle: { color: "#262626" } }, axisLabel: { color: "#b3b3b3", formatter: "{value}%" }, splitLine: { lineStyle: { color: "#1a1a1a" } } },
    yAxis: { type: "category", data: sorted.map((d) => d.label), axisLine: { lineStyle: { color: "#262626" } }, axisLabel: { color: "#b3b3b3" }, splitLine: { show: false } },
    series: [{
      type: "bar",
      data: sorted.map((d) => ({ value: Math.round(d.value * 1000) / 10, itemStyle: { color: "#3b82f6" } })),
      label: { show: true, position: "right", color: "#b3b3b3", formatter: "{c}%" },
    }],
  };
}

function buildPieChartOption(data: { name: string; value: number }[]): import("echarts").EChartsOption {
  return {
    tooltip: { trigger: "item", formatter: "{b}: {d}%" },
    series: [{
      type: "pie",
      radius: ["40%", "70%"],
      data: data.map((d) => ({ name: d.name, value: Math.round(d.value * 1000) / 10 })),
      label: { color: "#b3b3b3" },
      itemStyle: { borderRadius: 4, borderColor: "#0a0a0a", borderWidth: 2 },
    }],
  };
}

/* ---------- main page ---------- */

export function StockDetailPage() {
  const { ticker } = useParams<{ ticker: string }>();
  const navigate = useNavigate();
  const tk = ticker?.toUpperCase() ?? "";
  const [indexTicker, setIndexTicker] = useState("");

  // Fetch stock context
  const ctx = useQuery<StockContext>({
    queryKey: ["stock-context", tk],
    queryFn: () => api<StockContext>(`/api/research/stock/${encodeURIComponent(tk)}`),
    enabled: !!tk,
    staleTime: 5 * 60_000,
  });

  const etfProfile = useQuery<EtfProfile>({
    queryKey: ["etf-profile", tk],
    queryFn: () => api<EtfProfile>(`/api/etf/${encodeURIComponent(tk)}/profile`),
    enabled: !!tk,
    staleTime: 60 * 60_000,
    retry: false,
  });

  const tracking = useQuery<TrackingStats>({
    queryKey: ["etf-tracking", tk, indexTicker],
    queryFn: () => api<TrackingStats>(`/api/etf/tracking?ticker=${encodeURIComponent(tk)}&index=${encodeURIComponent(indexTicker)}`),
    enabled: !!tk && !!indexTicker,
    staleTime: 60 * 60_000,
    retry: false,
  });

  // Fetch multi-horizon verdicts
  const verdictsQ = useQuery<VerdictsResponse>({
    queryKey: ["stock-verdicts", tk],
    queryFn: () => api<VerdictsResponse>(`/api/research/stock/${encodeURIComponent(tk)}/verdicts`),
    enabled: !!tk,
    staleTime: 30 * 60_000,
  });

  // Generate / refresh report
  const reportMut = useMutation<ResearchReportResponse>({
    mutationFn: () =>
      api<ResearchReportResponse>(`/api/research/stock/${encodeURIComponent(tk)}/report`, { method: "POST" }),
    onSuccess: () => ctx.refetch(),
  });

  // Risk profile
  type RiskProfileResponse = { risk_profile: string };
  const riskProfile = useQuery<RiskProfileResponse>({
    queryKey: ["risk-profile"],
    queryFn: () => api<RiskProfileResponse>("/api/research/risk-profile"),
  });
  const updateProfile = useMutation<RiskProfileResponse, Error, string>({
    mutationFn: (profile: string) =>
      api<RiskProfileResponse>("/api/research/risk-profile", { method: "PUT", body: JSON.stringify({ risk_profile: profile }) }),
    onSuccess: () => {
      riskProfile.refetch();
      toast.success("Risk profile updated");
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const data = ctx.data;
  const quote = data?.quote;
  const price = quote?.price as number | undefined;
  const newsItems = data?.news ?? [];
  const fundamentals = data?.fundamentals as Record<string, unknown> | undefined;
  const report = data?.report;
  const chartData = data?.price_history ?? [];
  const verdicts = verdictsQ.data?.verdicts ?? [];

  const riskProfileVal = riskProfile.data?.risk_profile ?? "moderate";
  const riskProfileColors: Record<string, string> = {
    conservative: "bg-blue-500/15 text-blue-400 border-blue-500/30",
    moderate: "bg-warn/15 text-warn border-warn/30",
    aggressive: "bg-danger/15 text-danger border-danger/30",
  };

  // Piotroski F-Score
  const piotroski = useQuery<PiotroskiResponse>({
    queryKey: ["piotroski", tk],
    queryFn: () => api<PiotroskiResponse>(`/api/research/stock/${encodeURIComponent(tk)}/piotroski`),
    enabled: !!tk,
    staleTime: 60 * 60_000,
  });
  const pScore = piotroski.data?.score;
  const pSignals = piotroski.data?.signals ?? [];
  const pTotal = piotroski.data?.max ?? 9;
  const pColor = pScore == null ? "text-text-muted" : pScore >= 7 ? "text-success" : pScore >= 4 ? "text-warn" : "text-danger";

  // Sentiment aggregation
  const sentiment = useQuery<SentimentResponse>({
    queryKey: ["sentiment", tk],
    queryFn: () => api<SentimentResponse>(`/api/research/stock/${encodeURIComponent(tk)}/sentiment`),
    enabled: !!tk,
    staleTime: 15 * 60_000,
  });
  const sentData = sentiment.data;
  const isEtf = etfProfile.data != null;
  const profile = etfProfile.data;

  return (
    <div className="space-y-5">
      <PageHeader
        level={2}
        title={tk}
        subtitle={quote?.name ? String(quote.name) : undefined}
        breadcrumb={[
          { label: "Watchlist", href: "/research/watchlist" },
          { label: tk },
        ]}
        actions={
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => navigate(-1)}>
              <ArrowLeft size={14} className="mr-1" /> Back
            </Button>
            <Button
              variant="accent"
              size="sm"
              onClick={() => reportMut.mutate()}
              disabled={reportMut.isPending}
            >
              <RefreshCw size={14} className={`mr-1 ${reportMut.isPending ? "animate-spin" : ""}`} />
              {report?.cached ? "Refresh report" : "Generate report"}
            </Button>
          </div>
        }
      />

      {/* Price hero */}
      <div className="flex items-end gap-4 flex-wrap">
        {ctx.isLoading ? (
          <div className="animate-pulse h-8 w-48 bg-surface-3 rounded" />
        ) : (
          <div>
            <span className="text-4xl font-display font-bold">{formatPrice(price)}</span>
          </div>
        )}
        {typeof quote?.currency === "string" && <span className="text-sm text-text-muted mb-1">{quote.currency}</span>}
        {data?.report?.generated_at && (
          <span className="text-xs text-text-muted mb-1 flex items-center gap-1">
            <Clock size={12} /> Report {timeAgo(data.report.generated_at)}
            {data.report.cached && <Badge variant="outline" className="text-[10px] ml-1">cached</Badge>}
          </span>
        )}
        {riskProfile.data && (
          <div className="flex items-center gap-1.5 mb-1">
            <span className="text-[11px] text-text-muted inline-flex items-center gap-1">Risk: <InfoTooltip text="Sets the risk tolerance used for AI analysis. Conservative = capital preservation, Aggressive = growth focus." side="right" /></span>
            {(["conservative", "moderate", "aggressive"] as const).map((p) => (
              <button
                key={p}
                onClick={() => updateProfile.mutate(p)}
                disabled={updateProfile.isPending || riskProfileVal === p}
                className={`text-[10px] px-2 py-0.5 rounded-full border cursor-pointer transition-opacity ${
                  riskProfileVal === p
                    ? riskProfileColors[p]
                    : "border-border text-text-muted hover:text-text-primary opacity-60 hover:opacity-100"
                }`}
              >
                {p}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Multi-Horizon Verdicts */}
      {(verdictsQ.isLoading || verdicts.length > 0) && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm flex items-center gap-2">
              <Target size={14} /> <span className="inline-flex items-center gap-1.5">Multi-Horizon Verdicts <InfoTooltip text="AI-generated price direction forecasts across multiple time horizons. Not financial advice." side="right" /></span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {verdictsQ.isLoading ? (
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                {[1, 2, 3].map((i) => (
                  <div key={i} className="rounded-md border border-border bg-surface-2 p-3 animate-pulse">
                    <div className="h-3 bg-surface-3 rounded w-1/2 mx-auto mb-2" />
                    <div className="h-5 bg-surface-3 rounded w-1/3 mx-auto mb-1" />
                    <div className="h-3 bg-surface-3 rounded w-1/4 mx-auto" />
                  </div>
                ))}
              </div>
            ) : (
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
                {verdicts.map((v) => (
                  <div key={v.horizon} className="rounded-md border border-border bg-surface-2 p-3 text-center space-y-1">
                    <div className="text-[11px] uppercase tracking-wider text-text-muted">{v.horizon}</div>
                    <div className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-semibold border ${verdictColor(v.verdict)}`}>
                      {verdictIcon(v.verdict)} {v.verdict}
                    </div>
                    <span className="inline-flex items-center gap-1 text-[11px] text-text-muted">
                      {Math.round(v.confidence * 100)}%
                      <InfoTooltip text="Confidence score 0-100% indicating the AI's conviction in this forecast." side="right" />
                    </span>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Main content tabs */}
      <Tabs defaultValue="chart">
        <TabsList>
          <TabsTrigger value="chart"><BarChart3 size={14} className="mr-1" /> Chart</TabsTrigger>
          <TabsTrigger value="report" className="gap-1"><CheckCircle2 size={14} /> Report<InfoTooltip text="AI-generated research report analyzing the stock's fundamentals, technicals, and risk factors." side="bottom" /></TabsTrigger>
          <TabsTrigger value="news"><Newspaper size={14} className="mr-1" /> News</TabsTrigger>
          <TabsTrigger value="fundamentals"><Target size={14} className="mr-1" /> Fundamentals</TabsTrigger>
          {isEtf && <TabsTrigger value="etf"><PieChart size={14} className="mr-1" /> ETF</TabsTrigger>}
        </TabsList>

        {/* Chart tab */}
        <TabsContent value="chart">
          <Card>
            <CardContent className="pt-6">
              {chartData.length > 0 ? (
                <EChart
                  option={buildCandlestickOption(chartData)}
                  height={420}
                  ariaLabel={`Price chart for ${tk}`}
                />
              ) : (
                <div className="h-[420px] flex items-center justify-center text-text-muted text-sm">
                  {ctx.isLoading ? "Loading price history…" : "No price data available"}
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* Report tab */}
        <TabsContent value="report">
          <Card>
            <CardContent className="pt-6">
              {reportMut.isPending ? (
                <div className="space-y-3">
                  <div className="h-4 bg-surface-2 rounded animate-pulse w-1/3" />
                  <div className="h-3 bg-surface-2 rounded animate-pulse w-full" />
                  <div className="h-3 bg-surface-2 rounded animate-pulse w-5/6" />
                  <div className="h-3 bg-surface-2 rounded animate-pulse w-2/3" />
                  <p className="text-xs text-text-muted pt-2">Generating AI report… this may take a minute.</p>
                </div>
              ) : report?.text ? (
                <article className="prose prose-invert prose-sm max-w-none">
                  <ReactMarkdown>{report.text}</ReactMarkdown>
                </article>
              ) : (
                <div className="text-center py-12">
                  <AlertTriangle size={32} className="mx-auto text-text-muted mb-3" />
                  <p className="text-sm text-text-muted mb-4">No report generated yet.</p>
                  <Button variant="accent" onClick={() => reportMut.mutate()} disabled={reportMut.isPending}>
                    Generate report
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        {/* News tab */}
        <TabsContent value="news">
          <div className="space-y-4">
            {/* Sentiment aggregation bar */}
            {(sentiment.isLoading || (sentData && sentData.total > 0)) && (
              <Card>
                <CardHeader>
                  <CardTitle className="text-sm flex items-center gap-2">
                    <Newspaper size={14} /> <span className="inline-flex items-center gap-1.5">Sentiment <InfoTooltip text="AI-evaluated sentiment from recent news, social media, and analyst reports over a rolling 21-day window." side="right" /></span>
                    {sentData && sentData.total > 0 && (
                      <span className="text-[10px] text-text-muted font-normal">({sentData.total} articles, 21d)</span>
                    )}
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  {sentiment.isLoading ? (
                    <div className="animate-pulse h-24 w-full bg-surface-3 rounded" />
                  ) : sentData ? (
                    <div className="space-y-3">
                    {/* Overall badge */}
                    <div className="flex items-center gap-3">
                      <span className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-sm font-semibold border ${
                        sentData.overall === "positive"
                          ? "bg-success/15 text-success border-success/30"
                          : sentData.overall === "negative"
                            ? "bg-danger/15 text-danger border-danger/30"
                            : "bg-surface-2 text-text-secondary border-border"
                      }`}>
                        {sentData.overall === "positive" ? "▲" : sentData.overall === "negative" ? "▼" : "●"} {sentData.overall}
                      </span>
                      <span className="text-xs text-text-muted">avg score: {formatNumber(sentData.avg_score, { digits: 3 })}</span>
                    </div>
                    {/* Stacked bar */}
                    <div className="h-3 rounded-full overflow-hidden flex bg-surface-2">
                      <div
                        className="h-full bg-success transition-all"
                        style={{ width: `${sentData.positive_pct * 100}%` }}
                        title={`Positive: ${formatPercent(sentData.positive_pct, { digits: 1 })}`}
                      />
                      <div
                        className="h-full bg-surface-3 transition-all"
                        style={{ width: `${sentData.neutral_pct * 100}%` }}
                        title={`Neutral: ${formatPercent(sentData.neutral_pct, { digits: 1 })}`}
                      />
                      <div
                        className="h-full bg-danger transition-all"
                        style={{ width: `${sentData.negative_pct * 100}%` }}
                        title={`Negative: ${formatPercent(sentData.negative_pct, { digits: 1 })}`}
                      />
                    </div>
                    <div className="flex items-center gap-4 text-[11px] text-text-muted">
                      <span><span className="inline-block w-2 h-2 rounded-full bg-success mr-1" />{`${formatPercent(sentData.positive_pct, { digits: 0 })} positive`}</span>
                      <span><span className="inline-block w-2 h-2 rounded-full bg-surface-3 mr-1" />{`${formatPercent(sentData.neutral_pct, { digits: 0 })} neutral`}</span>
                      <span><span className="inline-block w-2 h-2 rounded-full bg-danger mr-1" />{`${formatPercent(sentData.negative_pct, { digits: 0 })} negative`}</span>
                    </div>
                    </div>
                  ) : null}
                </CardContent>
              </Card>
            )}

            {/* Article list */}
            <Card>
              <CardContent className="pt-6">
                {newsItems.length === 0 ? (
                  <p className="text-sm text-text-muted text-center py-8">No recent news.</p>
                ) : (
                  <div className="space-y-3">
                    {newsItems.map((n, i) => (
                      <div key={i} className="rounded-md border border-border bg-surface-2 p-4">
                        <div className="flex items-start justify-between gap-3">
                          <div className="flex-1 min-w-0">
                            <div className="text-sm font-medium leading-snug">{n.title}</div>
                            <div className="text-xs text-text-muted mt-1">
                              {n.source} · {timeAgo(n.published_at)}
                            </div>
                          </div>
                          <SentimentBadge label={n.sentiment_label} score={n.sentiment_score} />
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        {/* Fundamentals tab */}
        <TabsContent value="fundamentals">
          <div className="space-y-4">
            {/* Piotroski F-Score card */}
            <Card>
              <CardHeader>
                <CardTitle className="text-sm flex items-center gap-2">
                  <Target size={14} /> <span className="inline-flex items-center gap-1.5">Fundamental Strength Score <InfoTooltip text="A 9-point fundamental strength score based on profitability, leverage, liquidity, and operating efficiency (1-9 scale)." side="right" /></span>
                  <span className="text-[10px] text-text-muted font-normal">(Piotroski-inspired, 0–{pTotal})</span>
                </CardTitle>
              </CardHeader>
              <CardContent>
                {piotroski.isLoading ? (
                  <div className="h-8 bg-surface-2 rounded animate-pulse w-1/4" />
                ) : pScore != null ? (
                  <div className="space-y-3">
                    <div className="flex items-baseline gap-3">
                      <span className={`text-3xl font-display font-bold ${pColor}`}>{pScore}</span>
                      <span className="text-sm text-text-muted">/ {pTotal}</span>
                      <span className={`text-sm font-medium ${pColor}`}>
                        {pScore >= 7 ? "Strong fundamentals" : pScore >= 4 ? "Mixed signals" : "Weak fundamentals"}
                      </span>
                    </div>
                    <div className="grid grid-cols-3 sm:grid-cols-5 lg:grid-cols-9 gap-2">
                      {pSignals.map((s) => (
                        <div
                          key={s.name}
                          className={`rounded-md border p-2 text-center ${
                            s.pass
                              ? "border-success/30 bg-success/10"
                              : "border-danger/30 bg-danger/10"
                          }`}
                        >
                          <div className="text-[10px] text-text-muted leading-tight">{s.name}</div>
                          <div className={`text-xs font-semibold mt-0.5 ${s.pass ? "text-success" : "text-danger"}`}>
                            {s.pass ? "✓" : "✗"}
                          </div>
                          {s.value != null && (
                            <div className="text-[9px] text-text-muted mt-0.5">
                              {typeof s.value === "number" ? (Math.abs(s.value) < 1 ? formatPercent(s.value, { digits: 1 }) : formatNumber(s.value, { digits: 2 })) : s.value}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                    <p className="text-[10px] text-text-muted italic">
                      Uses proxy metrics from available provider data. Scores ≥7 indicate strong fundamentals; ≤3 suggest caution.
                    </p>
                  </div>
                ) : (
                  <p className="text-sm text-text-muted">No fundamental data available for scoring.</p>
                )}
              </CardContent>
            </Card>

            {/* Raw fundamentals grid */}
            <Card>
              <CardContent className="pt-6">
                {fundamentals && Object.keys(fundamentals).length > 0 ? (
                  <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                    {Object.entries(fundamentals).map(([k, v]) => (
                      <div key={k} className="rounded-md border border-border bg-surface-2 p-3">
                        <div className="text-[11px] uppercase tracking-wider text-text-muted">{k.replace(/_/g, " ")}</div>
                        <div className="text-sm font-semibold mt-1">
                          {typeof v === "number" ? formatNumber(v, { digits: 2, minDigits: 0 }) : String(v ?? "—")}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-text-muted text-center py-8">No fundamental data available.</p>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        {/* ETF tab */}
        {isEtf && (
          <TabsContent value="etf">
            <div className="space-y-4">
              {/* Profile info */}
              <Card>
                <CardHeader>
                  <CardTitle className="text-sm flex items-center gap-2">
                    <PieChart size={14} /> ETF Profile
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  {etfProfile.isLoading ? (
                    <div className="animate-pulse h-24 w-full bg-surface-3 rounded" />
                  ) : profile ? (
                    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
                      <div className="rounded-md border border-border bg-surface-2 p-3">
                        <div className="text-[11px] uppercase tracking-wider text-text-muted">TER</div>
                        <div className="text-sm font-semibold mt-1">{formatPercentPoints(profile.ter, { digits: 2 })}</div>
                      </div>
                      <div className="rounded-md border border-border bg-surface-2 p-3">
                        <div className="text-[11px] uppercase tracking-wider text-text-muted">Size</div>
                        <div className="text-sm font-semibold mt-1">{formatCompact(profile.size)}</div>
                      </div>
                      <div className="rounded-md border border-border bg-surface-2 p-3">
                        <div className="text-[11px] uppercase tracking-wider text-text-muted">Domicile</div>
                        <div className="text-sm font-semibold mt-1">{profile.domicile ?? "—"}</div>
                      </div>
                      <div className="rounded-md border border-border bg-surface-2 p-3">
                        <div className="text-[11px] uppercase tracking-wider text-text-muted">UCITS</div>
                        <div className="text-sm font-semibold mt-1">{profile.ucits ? "Yes" : "No"}</div>
                      </div>
                      <div className="rounded-md border border-border bg-surface-2 p-3">
                        <div className="text-[11px] uppercase tracking-wider text-text-muted">Replication</div>
                        <div className="text-sm font-semibold mt-1">{profile.replication ?? "—"}</div>
                      </div>
                    </div>
                  ) : (
                    <p className="text-sm text-text-muted text-center py-8">No ETF profile data available.</p>
                  )}
                </CardContent>
              </Card>

              {/* Top-10 holdings */}
              {profile && profile.top_holdings.length > 0 && (
                <Card>
                  <CardHeader>
                    <CardTitle className="text-sm">Top 10 Holdings</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <EChart
                      option={buildBarChartOption(profile.top_holdings.map((h) => ({ label: h.ticker, value: h.weight })))}
                      height={320}
                      ariaLabel={`Top 10 holdings for ${tk}`}
                    />
                  </CardContent>
                </Card>
              )}

              {/* Sector & Region distribution */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {profile && Object.keys(profile.sectors).length > 0 && (
                  <Card>
                    <CardHeader>
                      <CardTitle className="text-sm">Sector Breakdown</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <EChart
                        option={buildPieChartOption(Object.entries(profile.sectors).map(([name, value]) => ({ name, value })))}
                        height={280}
                        ariaLabel={`Sector breakdown for ${tk}`}
                      />
                    </CardContent>
                  </Card>
                )}
                {profile && Object.keys(profile.regions).length > 0 && (
                  <Card>
                    <CardHeader>
                      <CardTitle className="text-sm">Geographic Breakdown</CardTitle>
                    </CardHeader>
                    <CardContent>
                      <EChart
                        option={buildPieChartOption(Object.entries(profile.regions).map(([name, value]) => ({ name, value })))}
                        height={280}
                        ariaLabel={`Geographic breakdown for ${tk}`}
                      />
                      {profile.regions_source && (
                        <p className="mt-2 text-xs text-text-muted">Country weights of the {profile.regions_source}</p>
                      )}
                    </CardContent>
                  </Card>
                )}
              </div>

              {/* Tracking stats */}
              <Card>
                <CardHeader>
                  <CardTitle className="text-sm flex items-center gap-2">
                    <Target size={14} /> Tracking Stats
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="flex items-center gap-2 mb-4">
                    <input
                      type="text"
                      value={indexTicker}
                      onChange={(e) => setIndexTicker(e.target.value.toUpperCase())}
                      placeholder="Index ticker (e.g. SPY)"
                      className="bg-surface-2 border border-border rounded px-3 py-1.5 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-1 focus:ring-accent w-48"
                    />
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => tracking.refetch()}
                      disabled={!indexTicker || tracking.isFetching}
                    >
                      <RefreshCw size={14} className={`mr-1 ${tracking.isFetching ? "animate-spin" : ""}`} />
                      Compare
                    </Button>
                  </div>
                  {tracking.data ? (
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                      <div className="rounded-md border border-border bg-surface-2 p-3">
                        <div className="text-[11px] uppercase tracking-wider text-text-muted">Tracking Error</div>
                        <div className="text-sm font-semibold mt-1">{tracking.data.tracking_error}%</div>
                      </div>
                      <div className="rounded-md border border-border bg-surface-2 p-3">
                        <div className="text-[11px] uppercase tracking-wider text-text-muted">Beta</div>
                        <div className="text-sm font-semibold mt-1">{tracking.data.beta}</div>
                      </div>
                      <div className="rounded-md border border-border bg-surface-2 p-3">
                        <div className="text-[11px] uppercase tracking-wider text-text-muted">R²</div>
                        <div className="text-sm font-semibold mt-1">{tracking.data.r_squared}</div>
                      </div>
                      <div className="rounded-md border border-border bg-surface-2 p-3">
                        <div className="text-[11px] uppercase tracking-wider text-text-muted">Return Diff</div>
                        <div className="text-sm font-semibold mt-1">{tracking.data.annualised_return_diff > 0 ? "+" : ""}{tracking.data.annualised_return_diff}%</div>
                      </div>
                    </div>
                  ) : tracking.isFetching ? (
                    <div className="animate-pulse h-16 w-full bg-surface-3 rounded" />
                  ) : (
                    <p className="text-sm text-text-muted">Enter an index ticker to calculate tracking statistics.</p>
                  )}
                </CardContent>
              </Card>
            </div>
          </TabsContent>
        )}
      </Tabs>
    </div>
  );
}
