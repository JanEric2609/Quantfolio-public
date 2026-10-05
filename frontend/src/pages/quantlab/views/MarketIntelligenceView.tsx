import { formatCurrency, formatNumber, formatPercent } from "../../../lib/format";
import { useState } from "react";
import { Activity, TrendingUp, BarChart3, AlertTriangle, Newspaper } from "lucide-react";
import { useMarketSentiment } from "../hooks/useMarketSentiment";
import { useMarketMacro } from "../hooks/useMarketMacro";
import { useMarketAnalyst } from "../hooks/useMarketAnalyst";

interface SentimentArticleItem {
  sentiment?: string;
  sentiment_label?: string;
  title?: string;
  headline?: string;
  [key: string]: unknown;
}

interface MacroIndicatorItem {
  name?: string;
  indicator?: string;
  value?: number | string;
  date?: string;
}

interface AnalystEstimateItem {
  analyst?: string;
  firm?: string;
  name?: string;
  target_price?: number;
  recommendation?: string;
}

export function MarketIntelligenceView() {
  const [ticker, setTicker] = useState("AAPL");

  const sentiment = useMarketSentiment(ticker);
  const macro = useMarketMacro();
  const analyst = useMarketAnalyst(ticker);

  const sentimentData = sentiment.data;
  const macroData = macro.data;
  const analystData = analyst.data;

  return (
    <div className="space-y-4">
      {/* Ticker input */}
      <div className="flex items-center gap-3">
        <label className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
          Ticker
        </label>
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          className="w-24 rounded-md border border-line bg-surface px-2 py-1 text-xs font-mono uppercase text-text-text-primary"
          style={{ fontFamily: '"JetBrains Mono", ui-monospace, monospace' }}
          placeholder="AAPL"
        />
      </div>

      {/* Section 1: Sentiment */}
      <div className="rounded-lg border border-line bg-surface p-4">
        <div className="flex items-center gap-2 mb-3">
          <Newspaper className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-semibold text-text-primary">News Sentiment</h3>
          {sentimentData?.provider && (
            <span className="ml-auto text-[10px] text-muted-foreground">
              via {sentimentData.provider}
            </span>
          )}
        </div>
        {sentiment.isLoading && (
          <p className="text-xs text-muted-foreground">Loading sentiment data…</p>
        )}
        {sentiment.isError && (
          <p className="text-xs text-warn">Sentiment data unavailable.</p>
        )}
        {sentimentData && (
          <div className="space-y-3">
            {/* Sentiment score bar */}
            <div className="flex items-center gap-3">
              <div className="flex-1 h-2 rounded-full bg-muted overflow-hidden">
                <div
                  className="h-full rounded-full transition-all"
                  style={{
                    width: `${Math.abs(sentimentData.summary?.score ?? 0) * 100}%`,
                    background: (sentimentData.summary?.score ?? 0) >= 0
                      ? "rgb(34, 197, 94)"
                      : "rgb(239, 68, 68)",
                  }}
                />
              </div>
              <span className="text-xs font-mono text-muted-foreground w-12 text-right">
                {(sentimentData.summary?.score ?? 0) >= 0 ? "+" : ""}
                {formatPercent(sentimentData.summary?.score ?? 0, { digits: 1 })}
              </span>
            </div>

            {/* Counts */}
            <div className="grid grid-cols-4 gap-2 text-center">
              <div className="rounded bg-success/10 p-2">
                <div className="text-lg font-semibold text-success">
                  {sentimentData.summary?.positive ?? 0}
                </div>
                <div className="text-[10px] text-muted-foreground uppercase">Positive</div>
              </div>
              <div className="rounded bg-surface-2 p-2">
                <div className="text-lg font-semibold text-text-secondary">
                  {sentimentData.summary?.neutral ?? 0}
                </div>
                <div className="text-[10px] text-muted-foreground uppercase">Neutral</div>
              </div>
              <div className="rounded bg-danger/10 p-2">
                <div className="text-lg font-semibold text-danger">
                  {sentimentData.summary?.negative ?? 0}
                </div>
                <div className="text-[10px] text-muted-foreground uppercase">Negative</div>
              </div>
              {/* Distinct from "Neutral" — these articles simply haven't been
                  scored by FinBERT yet (e.g. sentiment_label is NULL). */}
              <div
                className="rounded p-2"
                style={{
                  backgroundImage:
                    "repeating-linear-gradient(45deg, rgba(148,163,184,0.14), rgba(148,163,184,0.14) 3px, transparent 3px, transparent 6px)",
                }}
                title="Not yet scored by FinBERT"
              >
                <div className="text-lg font-semibold text-muted-foreground">
                  {sentimentData.summary?.unscored ?? 0}
                </div>
                <div className="text-[10px] text-muted-foreground uppercase">Unscored</div>
              </div>
            </div>

            {/* Recent headlines */}
            {sentimentData.articles?.length > 0 && (
              <div className="space-y-1 max-h-48 overflow-y-auto">
                {sentimentData.articles.slice(0, 8).map((article: SentimentArticleItem, i: number) => (
                  <div key={i} className="flex items-start gap-2 text-xs text-muted-foreground py-1 border-b border-line/50 last:border-0">
                    <span className="shrink-0 mt-0.5">
                      {(() => {
                        const raw = article.sentiment ?? article.sentiment_label;
                        // null/undefined = FinBERT hasn't scored this article
                        // yet — render distinctly from a genuine "neutral"
                        // verdict so a dead scoring pipeline is visible in
                        // the UI rather than looking identical to "neutral".
                        if (raw == null) {
                          return <span className="text-text-muted/50" title="Not yet scored">○</span>;
                        }
                        const label = String(raw).toLowerCase();
                        if (label.includes("pos") || label.includes("bull")) return <span className="text-success">●</span>;
                        if (label.includes("neg") || label.includes("bear")) return <span className="text-danger">●</span>;
                        return <span className="text-text-muted" title="Neutral">●</span>;
                      })()}
                    </span>
                    <span className="line-clamp-2">{article.title || article.headline || JSON.stringify(article).slice(0, 120)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Section 2: Macro Indicators */}
      <div className="rounded-lg border border-line bg-surface p-4">
        <div className="flex items-center gap-2 mb-3">
          <Activity className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-semibold text-text-primary">Macro Indicators</h3>
          {macroData?.provider && (
            <span className="ml-auto text-[10px] text-muted-foreground">
              via {macroData.provider}
            </span>
          )}
        </div>
        {macro.isLoading && (
          <p className="text-xs text-muted-foreground">Loading macro data…</p>
        )}
        {macro.isError && (
          <p className="text-xs text-warn">Macro data unavailable.</p>
        )}
        {macroData && (
          <div className="space-y-2">
            {macroData.indicators?.length === 0 && (
              <p className="text-xs text-muted-foreground">No macro indicators available.</p>
            )}
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
              {(Array.isArray(macroData.indicators) ? macroData.indicators : []).map((ind: MacroIndicatorItem, i: number) => (
                <div key={i} className="rounded bg-muted/50 p-2">
                  <div className="text-[10px] text-muted-foreground uppercase truncate">
                    {ind.name ?? ind.indicator ?? "Unknown"}
                  </div>
                  <div className="text-sm font-semibold font-mono text-text-primary">
                    {ind.value != null ? formatNumber(Number(ind.value), { digits: 2, minDigits: 0 }) : "—"}
                  </div>
                  {ind.date && (
                    <div className="text-[10px] text-muted-foreground">{ind.date}</div>
                  )}
                </div>
              ))}
            </div>
            {macroData.warnings?.length > 0 && (
              <div className="flex items-start gap-1.5 mt-2">
                <AlertTriangle className="h-3 w-3 text-warn mt-0.5 shrink-0" />
                <p className="text-[10px] text-warn">
                  {macroData.warnings[0]}
                </p>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Section 3: Analyst Estimates */}
      <div className="rounded-lg border border-line bg-surface p-4">
        <div className="flex items-center gap-2 mb-3">
          <BarChart3 className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-semibold text-text-primary">Analyst Estimates</h3>
          {analystData?.provider && (
            <span className="ml-auto text-[10px] text-muted-foreground">
              via {analystData.provider}
            </span>
          )}
        </div>
        {analyst.isLoading && (
          <p className="text-xs text-muted-foreground">Loading analyst estimates…</p>
        )}
        {analyst.isError && (
          <p className="text-xs text-warn">Analyst estimates unavailable.</p>
        )}
        {analystData && (
          <div className="space-y-3">
            {/* Summary cards */}
            {analystData.summary?.total_analysts > 0 && (
              <div className="grid grid-cols-4 gap-2 text-center">
                <div className="rounded bg-muted/50 p-2">
                  <div className="text-lg font-semibold text-text-primary">
                    {analystData.summary.total_analysts}
                  </div>
                  <div className="text-[10px] text-muted-foreground uppercase">Total</div>
                </div>
                <div className="rounded bg-success/10 p-2">
                  <div className="text-lg font-semibold text-success">
                    {analystData.summary.buy}
                  </div>
                  <div className="text-[10px] text-muted-foreground uppercase">Buy</div>
                </div>
                <div className="rounded bg-surface-2 p-2">
                  <div className="text-lg font-semibold text-text-secondary">
                    {analystData.summary.hold}
                  </div>
                  <div className="text-[10px] text-muted-foreground uppercase">Hold</div>
                </div>
                <div className="rounded bg-danger/10 p-2">
                  <div className="text-lg font-semibold text-danger">
                    {analystData.summary.sell}
                  </div>
                  <div className="text-[10px] text-muted-foreground uppercase">Sell</div>
                </div>
              </div>
            )}

            {/* Individual estimates */}
            {analystData.estimates?.length > 0 && (
              <div className="space-y-1 max-h-48 overflow-y-auto">
                {analystData.estimates.slice(0, 10).map((est: AnalystEstimateItem, i: number) => (
                  <div key={i} className="flex items-center justify-between text-xs py-1 border-b border-line/50 last:border-0">
                    <span className="text-muted-foreground truncate">
                      {est.analyst || est.firm || est.name || `Analyst ${i + 1}`}
                    </span>
                    <div className="flex items-center gap-2 shrink-0">
                      {est.target_price != null && (
                        <span className="font-mono text-text-primary">
                          {formatCurrency(Number(est.target_price), "USD", { digits: 0 })}
                        </span>
                      )}
                      {est.recommendation && (
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                            String(est.recommendation).toLowerCase().includes("buy")
                              ? "bg-success/20 text-success"
                              : String(est.recommendation).toLowerCase().includes("sell")
                                ? "bg-danger/20 text-danger"
                                : "bg-surface-2 text-text-secondary"
                          }`}
                        >
                          {est.recommendation}
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {analystData.estimates?.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No analyst estimates available for {ticker}.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
